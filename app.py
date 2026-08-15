"""
Notre Histoire — app couple, web mobile-first.
Application autonome, totalement isolee d'AqGK.

Patches deployes :
- v0    : bootstrap (Hello + healthz)
- v1.0  : couple (users, couples, invitations, login/logout/onboarding/invite)

Patches futurs :
- v1.1  : carnets (CRUD fiches)
- v1.2  : album (photos + captions)
- v1.3  : apercu livre + export PDF
- v1.4  : profil
"""
import os
import io
import sqlite3
import secrets
import hashlib
import logging
import traceback
import zipfile
import smtplib
import shutil
from functools import wraps
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication

import bcrypt
import qrcode
import qrcode.image.svg as qrsvg
from PIL import Image, ExifTags

# v5.10 — HEIC/HEIF (photos iPhone envoyees sans conversion Safari).
# Optionnel : sans pillow-heif l'app fonctionne, mais un .heic uploadera
# en erreur — le log le dit clairement (pas de repli muet, R4).
try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
    _HEIF_OK = True
except ImportError:
    _HEIF_OK = False
from flask import (
    Flask, render_template, request, redirect, url_for, session,
    jsonify, abort, flash, send_from_directory
)

logging.basicConfig(level=logging.INFO,
    format='[%(asctime)s] %(levelname)s %(name)s: %(message)s')
log = logging.getLogger('histoire')
if not _HEIF_OK:
    log.warning("pillow-heif absent : les uploads .heic (iPhone) echoueront "
                "avec une erreur visible cote client")

# ── Config ────────────────────────────────────────────────────────────
APP_VERSION = "5.18"
# v5.16 — version des ASSETS (style.css, pins.js) servie en ?v= : un
# telephone qui garde l'ancien CSS/JS en cache apres un deploiement rend
# tous les correctifs invisibles (regle D3 etendue). A BUMPER a chaque
# lot qui touche static/.
ASSET_V = "5.18"
DB_PATH = os.environ.get('DATABASE_PATH', os.path.join(os.path.dirname(__file__), 'carnet.db'))
UPLOAD_DIR = os.environ.get('UPLOAD_DIR', os.path.join(os.path.dirname(DB_PATH), 'uploads'))
BACKUP_DIR = os.environ.get('BACKUP_DIR', os.path.join(os.path.dirname(DB_PATH), 'backups'))
SECRET_KEY = os.environ.get('SECRET_KEY') or secrets.token_urlsafe(32)
INVITATION_TTL_DAYS = 14

# Backup
BACKUP_TOKEN = os.environ.get('BACKUP_TOKEN', '')
BACKUP_KEEP = int(os.environ.get('BACKUP_KEEP', '7'))
BACKUP_EMAIL_TO = os.environ.get('BACKUP_EMAIL_TO', 'arthur.kembellec@gmail.com')

# SMTP (optionnel — si non configure, backup local uniquement)
SMTP_HOST = os.environ.get('SMTP_HOST', '')
SMTP_PORT = int(os.environ.get('SMTP_PORT', '587'))
SMTP_USER = os.environ.get('SMTP_USER', '')
SMTP_PASS = os.environ.get('SMTP_PASS', '')
SMTP_FROM = os.environ.get('SMTP_FROM', SMTP_USER)

# Web Push (PWA notifications)
VAPID_PUBLIC_KEY = os.environ.get('VAPID_PUBLIC_KEY', '')
VAPID_PRIVATE_KEY = os.environ.get('VAPID_PRIVATE_KEY', '')
VAPID_SUBJECT = os.environ.get('VAPID_SUBJECT', 'mailto:arthur.kembellec@gmail.com')

# Admins (pour pages /admin/*)
ADMIN_EMAILS = set(
    e.strip().lower()
    for e in os.environ.get('ADMIN_EMAILS', 'arthur.kembellec@gmail.com').split(',')
    if e.strip()
)

os.makedirs(os.path.dirname(DB_PATH) or '.', exist_ok=True)
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(BACKUP_DIR, exist_ok=True)

app = Flask(__name__)
app.config['SECRET_KEY'] = SECRET_KEY
app.config['MAX_CONTENT_LENGTH'] = 2 * 1024 * 1024 * 1024  # 2 Go upload (videos lourdes)
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_HTTPONLY'] = True
# SECURE = True uniquement en prod (Railway sert HTTPS).
app.config['SESSION_COOKIE_SECURE'] = bool(os.environ.get('RAILWAY_ENVIRONMENT'))
# Brief 08 §1 : rester connecte — session persistante 90 jours
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=90)


@app.before_request
def _make_session_permanent():
    """Brief 08 §1 : tant que l'utilisateur est logge, sa session reste
    valide 90 jours et se prolonge a chaque requete."""
    if session.get('uid'):
        session.permanent = True


@app.before_request
def _session_apres_reset():
    """v5.4 : une session ouverte AVANT le dernier changement de mot de passe
    tombe. Sans ca, reinitialiser ne chasse pas celui qui est deja entre.

    Tolerant par construction : une session d'avant la v5.4 n'a pas de repere,
    on lui en pose un au lieu de la fermer — personne n'est deconnecte par le
    deploiement, et la revocation vaut pour tout ce qui s'ouvre ensuite."""
    uid = session.get('uid')
    if not uid:
        return
    try:
        r = query("SELECT pw_changed_at FROM users WHERE id=?", (uid,), one=True)
    except Exception:
        return                      # migration pas encore passee : on ne bloque rien
    if not r:
        return
    change = str(r['pw_changed_at'] or '')[:19]
    if not change:
        return
    depuis = str(session.get('pw_at') or '')[:19]
    if not depuis:
        session['pw_at'] = change   # session d'avant : on l'adopte, on ne la casse pas
        return
    if depuis < change:
        session.clear()


# ── DB helpers ────────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _migrate_album_pages_video(conn):
    """v4.4 — recree album_pages pour autoriser type='video'.
    C'etait LE bug des videos invisibles : le CHECK type IN ('photo','text')
    faisait echouer l'INSERT de la page a la fin de chaque upload video
    (la video etait sauvee, sa page jamais creee). Idempotente, dynamique :
    reprend le schema live (colonnes ajoutees par ALTER comprises)."""
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='album_pages'"
        ).fetchone()
        if not row or not row[0]:
            return
        sql = row[0]
        if "'video'" in sql:
            return  # deja migre
        if "('photo','text')" not in sql:
            log.warning("album_pages : CHECK inattendu, migration video sautee")
            return
        log.info("v4.4 migration album_pages : ajout type 'video'")
        new_sql = sql.replace("('photo','text')", "('photo','text','video')")
        import re as _re
        new_sql = _re.sub(r'CREATE TABLE (IF NOT EXISTS )?"?album_pages"?',
                          'CREATE TABLE album_pages_new', new_sql, count=1)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(album_pages)")]
        col_list = ', '.join(f'"{c}"' for c in cols)
        conn.executescript(f"""
            PRAGMA foreign_keys=OFF;
            {new_sql};
            INSERT INTO album_pages_new ({col_list})
                SELECT {col_list} FROM album_pages;
            DROP TABLE album_pages;
            ALTER TABLE album_pages_new RENAME TO album_pages;
            CREATE INDEX IF NOT EXISTS idx_pages_carnet ON album_pages(carnet_id, position);
            PRAGMA foreign_keys=ON;
        """)
        conn.commit()
    except Exception as e:
        log.error("migration album_pages video: %s", e)


def _migrate_trajets_v52(conn):
    """v5.2 — la journee devient une suite de BLOCS.

    La v5.1 stockait des etapes plates : trajet_steps(carnet_id, day, position,
    item_id) avec UNIQUE(carnet_id, day, item_id). Deux besoins la cassent :
    plusieurs trajets par jour (chacun son mode) et la meme epingle deux fois
    dans un trajet (l'aller-retour A->B->A). SQLite ne sait pas retirer une
    contrainte -> reconstruction.

    Un bloc de la v5.1 = un jour entier -> on cree UN trajet 'car' par
    (carnet_id, day) et on y reverse ses etapes dans l'ordre.

    Idempotente : on ne reconstruit que si l'ancienne forme est encore la.
    Les comptes sont verifies AVANT de detruire quoi que ce soit ; en cas
    d'ecart on abandonne en laissant la base intacte et on le DIT (l'app
    retombe alors sur la derivation planned_day, cf. _trajet_blocs)."""
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(trajet_steps)")]
        if not cols:
            return                      # base neuve : init_db cree la forme v5.2
        if 'trajet_id' in cols:
            return                      # deja en v5.2
        n_avant = conn.execute("SELECT COUNT(*) FROM trajet_steps").fetchone()[0]
        log.info("v5.2 migration trajet_steps : %d etape(s) a reprendre", n_avant)
        conn.executescript("""
            PRAGMA foreign_keys=OFF;
            CREATE TABLE IF NOT EXISTS trajets (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                carnet_id  INTEGER NOT NULL REFERENCES carnets(id) ON DELETE CASCADE,
                day        INTEGER NOT NULL,
                ordre      INTEGER NOT NULL DEFAULT 0,
                mode       TEXT NOT NULL DEFAULT 'car',
                heure      TEXT NOT NULL DEFAULT ''
            );
            -- on n'entre ici que depuis la v5.1, ou l'app n'ecrivait jamais
            -- dans trajets : ce qui s'y trouve vient d'une tentative avortee.
            DELETE FROM trajets;
            CREATE TABLE trajet_steps_v52 (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                trajet_id  INTEGER NOT NULL REFERENCES trajets(id) ON DELETE CASCADE,
                position   INTEGER NOT NULL DEFAULT 0,
                item_id    INTEGER NOT NULL REFERENCES carnet_items(id) ON DELETE CASCADE
            );
            INSERT INTO trajets (carnet_id, day, ordre, mode, heure)
                SELECT DISTINCT carnet_id, day, 0, 'car', '' FROM trajet_steps;
            INSERT INTO trajet_steps_v52 (trajet_id, position, item_id)
                SELECT t.id, ts.position, ts.item_id
                FROM trajet_steps ts
                JOIN trajets t ON t.carnet_id = ts.carnet_id AND t.day = ts.day;
        """)
        n_apres = conn.execute("SELECT COUNT(*) FROM trajet_steps_v52").fetchone()[0]
        if n_apres != n_avant:
            conn.executescript("DROP TABLE trajet_steps_v52; PRAGMA foreign_keys=ON;")
            conn.commit()
            log.error("v5.2 migration ABANDONNEE : %d etapes avant, %d apres. "
                      "Base laissee en v5.1, le planning retombe sur planned_day.",
                      n_avant, n_apres)
            return
        conn.executescript("""
            DROP TABLE trajet_steps;
            ALTER TABLE trajet_steps_v52 RENAME TO trajet_steps;
            CREATE INDEX IF NOT EXISTS idx_trajets_carnet ON trajets(carnet_id, day, ordre);
            CREATE INDEX IF NOT EXISTS idx_trajet_steps ON trajet_steps(trajet_id, position);
            PRAGMA foreign_keys=ON;
        """)
        conn.commit()
        log.info("v5.2 migration trajet_steps : OK (%d etapes reprises)", n_apres)
    except Exception as e:
        log.error("v5.2 migration trajet_steps ECHEC : %s\n%s", e, traceback.format_exc())


def _migrate_carnets_souhait(conn):
    """v1.4 — recree la table carnets pour autoriser type='souhait'
    et ajouter la colonne parent_souhait_id. Idempotente."""
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='carnets'"
        ).fetchone()
        if not row:
            return  # table pas encore creee, rien a faire
        sql = row[0] or ''
        if "'souhait'" in sql and 'parent_souhait_id' in sql:
            return  # deja migre
        log.info("v1.4 migration carnets : ajout 'souhait' + parent_souhait_id")
        conn.executescript("""
            PRAGMA foreign_keys=OFF;
            CREATE TABLE carnets_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                couple_id INTEGER NOT NULL REFERENCES couples(id) ON DELETE CASCADE,
                title TEXT NOT NULL,
                type TEXT NOT NULL DEFAULT 'voyage'
                    CHECK(type IN ('voyage','restaurant','sortie','souhait','autre')),
                location TEXT DEFAULT '',
                date_start DATE,
                date_end DATE,
                cover_photo_id INTEGER,
                status TEXT NOT NULL DEFAULT 'draft'
                    CHECK(status IN ('draft','active','locked','archived')),
                created_by INTEGER NOT NULL REFERENCES users(id),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                deleted_at TIMESTAMP DEFAULT NULL,
                parent_souhait_id INTEGER REFERENCES carnets(id) ON DELETE SET NULL
            );
            INSERT INTO carnets_new (id, couple_id, title, type, location,
                date_start, date_end, cover_photo_id, status, created_by,
                created_at, updated_at, deleted_at)
            SELECT id, couple_id, title, type, location, date_start, date_end,
                cover_photo_id, status, created_by, created_at, updated_at, deleted_at
            FROM carnets;
            DROP TABLE carnets;
            ALTER TABLE carnets_new RENAME TO carnets;
            CREATE INDEX IF NOT EXISTS idx_carnets_couple ON carnets(couple_id);
            CREATE INDEX IF NOT EXISTS idx_carnets_status ON carnets(status);
            CREATE INDEX IF NOT EXISTS idx_carnets_parent ON carnets(parent_souhait_id);
            PRAGMA foreign_keys=ON;
        """)
        conn.commit()
    except Exception as e:
        log.warning("v1.4 migration carnets ECHEC (skip): %s", e)


def init_db():
    """
    Migrations idempotentes. Toute nouvelle table / colonne s'ajoute ici,
    en respectant l'ordre (les FK dependantes apres leurs cibles).
    """
    conn = get_db()

    # v1.4 — migration speciale carnets (CHECK strict + ajout colonne)
    _migrate_carnets_souhait(conn)
    # v4.4 — migration speciale album_pages (CHECK -> autorise 'video')
    _migrate_album_pages_video(conn)
    # v5.2 — trajet_steps v5.1 (plat, UNIQUE) -> blocs (trajets + etapes)
    _migrate_trajets_v52(conn)

    migrations = [
        # ── v1.0 — couple : users + couples + invitations ─────────────
        """CREATE TABLE IF NOT EXISTS couples (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            name          TEXT DEFAULT '',
            created_by    INTEGER,
            created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            email         TEXT NOT NULL UNIQUE,
            display_name  TEXT NOT NULL DEFAULT '',
            avatar_b64    TEXT DEFAULT '',
            password_hash TEXT NOT NULL,
            couple_id     INTEGER REFERENCES couples(id) ON DELETE SET NULL,
            created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TABLE IF NOT EXISTS invitations (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            token         TEXT NOT NULL UNIQUE,
            couple_id     INTEGER NOT NULL REFERENCES couples(id) ON DELETE CASCADE,
            email         TEXT DEFAULT '',
            expires_at    TIMESTAMP NOT NULL,
            utilise       INTEGER DEFAULT 0,
            created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
        "CREATE INDEX IF NOT EXISTS idx_users_couple ON users(couple_id)",
        "CREATE INDEX IF NOT EXISTS idx_invit_couple ON invitations(couple_id)",
        # ── v1.1 — carnets : fiche d'un voyage / restau / sortie ──────
        """CREATE TABLE IF NOT EXISTS carnets (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            couple_id     INTEGER NOT NULL REFERENCES couples(id) ON DELETE CASCADE,
            title         TEXT NOT NULL,
            type          TEXT NOT NULL DEFAULT 'voyage'
                          CHECK(type IN ('voyage','restaurant','sortie','autre')),
            location      TEXT DEFAULT '',
            date_start    DATE,
            date_end      DATE,
            cover_photo_id INTEGER,
            status        TEXT NOT NULL DEFAULT 'draft'
                          CHECK(status IN ('draft','active','locked','archived')),
            created_by    INTEGER NOT NULL REFERENCES users(id),
            created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            deleted_at    TIMESTAMP DEFAULT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_carnets_couple ON carnets(couple_id)",
        "CREATE INDEX IF NOT EXISTS idx_carnets_status ON carnets(status)",
        # ── v1.2 — album : photos + pages (texte ou photo) ────────────
        """CREATE TABLE IF NOT EXISTS photos (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            couple_id     INTEGER NOT NULL REFERENCES couples(id) ON DELETE CASCADE,
            file_path     TEXT NOT NULL,
            thumb_path    TEXT NOT NULL,
            width         INTEGER, height INTEGER,
            taken_at      TIMESTAMP,
            location      TEXT DEFAULT '',
            added_by      INTEGER NOT NULL REFERENCES users(id),
            added_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TABLE IF NOT EXISTS album_pages (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            carnet_id     INTEGER NOT NULL REFERENCES carnets(id) ON DELETE CASCADE,
            type          TEXT NOT NULL CHECK(type IN ('photo','text')),
            position      INTEGER NOT NULL DEFAULT 0,
            photo_id      INTEGER REFERENCES photos(id) ON DELETE CASCADE,
            caption       TEXT DEFAULT '',
            text_content  TEXT DEFAULT '',
            added_by      INTEGER REFERENCES users(id),
            created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
        "CREATE INDEX IF NOT EXISTS idx_pages_carnet ON album_pages(carnet_id, position)",
        "CREATE INDEX IF NOT EXISTS idx_photos_couple ON photos(couple_id)",
        # ── v1.2.2 — GPS sur photos + notes en marge sur pages ────────
        "ALTER TABLE photos ADD COLUMN gps_lat REAL",
        "ALTER TABLE photos ADD COLUMN gps_lng REAL",
        "ALTER TABLE album_pages ADD COLUMN is_margin INTEGER DEFAULT 0",
        "CREATE INDEX IF NOT EXISTS idx_photos_gps ON photos(couple_id, gps_lat, gps_lng)",
        # ── v1.3 — multi-espaces (renommage logique : couple = espace) ─
        "ALTER TABLE couples ADD COLUMN kind TEXT DEFAULT 'couple'",
        """CREATE TABLE IF NOT EXISTS espace_members (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            espace_id   INTEGER NOT NULL REFERENCES couples(id) ON DELETE CASCADE,
            user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            role        TEXT DEFAULT 'member',
            joined_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(espace_id, user_id)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_em_user ON espace_members(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_em_espace ON espace_members(espace_id)",
        # Backfill : chaque user avec couple_id devient membre de cet espace
        """INSERT OR IGNORE INTO espace_members (espace_id, user_id, role)
           SELECT u.couple_id, u.id,
                  CASE WHEN c.created_by = u.id THEN 'owner' ELSE 'member' END
           FROM users u JOIN couples c ON c.id = u.couple_id
           WHERE u.couple_id IS NOT NULL""",
        # ── v1.4.1 — videos (avec poster extrait cote client + scan_token public)
        """CREATE TABLE IF NOT EXISTS videos (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            couple_id   INTEGER NOT NULL REFERENCES couples(id) ON DELETE CASCADE,
            file_path   TEXT NOT NULL,
            poster_path TEXT NOT NULL,
            duration_s  REAL,
            width       INTEGER, height INTEGER,
            taken_at    TIMESTAMP,
            gps_lat     REAL, gps_lng REAL,
            scan_token  TEXT UNIQUE NOT NULL,
            added_by    INTEGER NOT NULL REFERENCES users(id),
            added_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
        "CREATE INDEX IF NOT EXISTS idx_videos_couple ON videos(couple_id)",
        "ALTER TABLE album_pages ADD COLUMN video_id INTEGER REFERENCES videos(id) ON DELETE SET NULL",
        # ── v1.4.2 — categorisation des souhaits ─────────────────────
        # Pour les carnets de type='souhait', categorie du futur voyage
        # (voyage / restaurant / sortie / autre).
        "ALTER TABLE carnets ADD COLUMN souhait_kind TEXT DEFAULT 'voyage'",
        # ── v1.2.5 — drag & drop : sort_mode 'chrono' (default) ou 'manual'
        "ALTER TABLE carnets ADD COLUMN sort_mode TEXT DEFAULT 'chrono'",
        # ── v1.6 — soft delete utilisateurs (fenetre 30j de recuperation)
        "ALTER TABLE users ADD COLUMN deleted_at TIMESTAMP DEFAULT NULL",
        # ── v2.0.1 — config PDF par carnet (layout + position marge)
        "ALTER TABLE carnets ADD COLUMN pdf_layout TEXT DEFAULT '1'",
        "ALTER TABLE carnets ADD COLUMN pdf_margin_position TEXT DEFAULT 'right'",
        # ── v2.1 — charte : couleur d'accent par espace ───────────────
        "ALTER TABLE couples ADD COLUMN accent TEXT DEFAULT 'terracotta'",
        # ── v2.3 — Album : regroupement chronologique automatique ────
        """CREATE TABLE IF NOT EXISTS album_sections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            carnet_id INTEGER NOT NULL REFERENCES carnets(id) ON DELETE CASCADE,
            level INTEGER NOT NULL CHECK(level IN (1, 2)),
            parent_section_id INTEGER REFERENCES album_sections(id) ON DELETE CASCADE,
            kind TEXT NOT NULL CHECK(kind IN ('day','location','mixed','unknown')),
            primary_label TEXT NOT NULL DEFAULT '',
            secondary_label TEXT DEFAULT '',
            part_of_day TEXT DEFAULT '',
            date_start TIMESTAMP,
            date_end TIMESTAMP,
            location_name TEXT DEFAULT '',
            location_lat REAL,
            location_lng REAL,
            photo_count INTEGER DEFAULT 0,
            position INTEGER NOT NULL DEFAULT 0,
            is_auto INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
        "CREATE INDEX IF NOT EXISTS idx_sections_carnet ON album_sections(carnet_id, position)",
        "CREATE INDEX IF NOT EXISTS idx_sections_parent ON album_sections(parent_section_id)",
        "ALTER TABLE album_pages ADD COLUMN section_id INTEGER REFERENCES album_sections(id) ON DELETE SET NULL",
        "ALTER TABLE album_pages ADD COLUMN manual_order INTEGER DEFAULT 0",
        "ALTER TABLE album_pages ADD COLUMN is_hidden INTEGER DEFAULT 0",
        "ALTER TABLE photos ADD COLUMN city_name TEXT DEFAULT ''",
        # ── Brief 08 §3 : reverse-geocoding lisible (Pays / Departement / Ville / Rue)
        "ALTER TABLE photos ADD COLUMN country TEXT DEFAULT ''",
        "ALTER TABLE photos ADD COLUMN state TEXT DEFAULT ''",
        "ALTER TABLE photos ADD COLUMN road TEXT DEFAULT ''",
        "ALTER TABLE photos ADD COLUMN address_full TEXT DEFAULT ''",
        """CREATE TABLE IF NOT EXISTS geo_cache (
            key         TEXT PRIMARY KEY,
            country     TEXT DEFAULT '',
            state       TEXT DEFAULT '',
            city        TEXT DEFAULT '',
            road        TEXT DEFAULT '',
            full        TEXT DEFAULT '',
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
        # ── v2.3 — Mise en page (Brief 05 §14-17) ─────────────────────
        "ALTER TABLE carnets ADD COLUMN default_photos_per_page INTEGER DEFAULT 1",
        "ALTER TABLE carnets ADD COLUMN default_page_margin REAL DEFAULT 15.0",
        "ALTER TABLE album_pages ADD COLUMN photos_per_page_override INTEGER",
        "ALTER TABLE album_pages ADD COLUMN page_margin_override REAL",
        "ALTER TABLE album_pages ADD COLUMN full_bleed_override INTEGER",
        # ── v2.4 — options carto PDF (Brief 06 §5.7)
        "ALTER TABLE carnets ADD COLUMN pdf_show_overview_map INTEGER DEFAULT 1",
        "ALTER TABLE carnets ADD COLUMN pdf_show_section_maps INTEGER DEFAULT 1",
        # ── v4.5 — reverie pre-planning : epingles typees, planning par jour,
        # notes rattachees a une epingle
        "ALTER TABLE carnet_items ADD COLUMN pin_kind TEXT DEFAULT ''",
        "ALTER TABLE carnet_items ADD COLUMN planned_day INTEGER",
        "ALTER TABLE carnet_items ADD COLUMN parent_item_id INTEGER REFERENCES carnet_items(id) ON DELETE SET NULL",
        # ── v4.2 — note de marge rattachee a une photo de l'album
        # (la note se place a cote de sa photo dans le rail, et sur la
        #  page de cette photo dans le livre)
        "ALTER TABLE album_pages ADD COLUMN anchor_page_id INTEGER REFERENCES album_pages(id) ON DELETE SET NULL",
        # ── v4.6 — marge dependante d'un EVENEMENT : photo (anchor_page_id)
        # OU etape du planning (anchor_item_id)
        "ALTER TABLE album_pages ADD COLUMN anchor_item_id INTEGER REFERENCES carnet_items(id) ON DELETE SET NULL",
        # ── v2.2 — Web Push : abonnements aux notifications PWA ──────
        """CREATE TABLE IF NOT EXISTS push_subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            espace_id INTEGER REFERENCES couples(id) ON DELETE CASCADE,
            endpoint TEXT NOT NULL,
            p256dh TEXT NOT NULL,
            auth TEXT NOT NULL,
            user_agent TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, endpoint)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_push_user ON push_subscriptions(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_push_espace ON push_subscriptions(espace_id)",
        # ── v2.0 — Histoire & Conversations ────────────────────────────
        """CREATE TABLE IF NOT EXISTS conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            espace_id INTEGER NOT NULL UNIQUE REFERENCES couples(id) ON DELETE CASCADE,
            archive_imported_at TIMESTAMP,
            archive_source TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TABLE IF NOT EXISTS chapters (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            position INTEGER NOT NULL DEFAULT 0,
            title TEXT NOT NULL DEFAULT '',
            headline TEXT DEFAULT '',
            date_label TEXT DEFAULT '',
            weekday_label TEXT DEFAULT '',
            featured_image_url TEXT DEFAULT '',
            image_caption TEXT DEFAULT ''
        )""",
        """CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            kind TEXT NOT NULL CHECK(kind IN ('archived','live')),
            chapter_id INTEGER REFERENCES chapters(id) ON DELETE SET NULL,
            sender_type TEXT CHECK(sender_type IN ('userA','userB','system','member') OR sender_type IS NULL),
            sender_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            sender_label TEXT DEFAULT '',
            body TEXT NOT NULL DEFAULT '',
            attachment_type TEXT,
            attachment_ref TEXT,
            sent_at TIMESTAMP NOT NULL,
            edited_at TIMESTAMP,
            deleted_at TIMESTAMP
        )""",
        "CREATE INDEX IF NOT EXISTS idx_msg_conv_sent ON messages(conversation_id, sent_at)",
        "CREATE INDEX IF NOT EXISTS idx_msg_chapter ON messages(chapter_id, sent_at)",
        # Backfill : 1 conversation par espace existant
        "INSERT OR IGNORE INTO conversations (espace_id) SELECT id FROM couples",
        # ── v1.4 — items des carnets de souhait (link/photo/note/lieu/budget)
        """CREATE TABLE IF NOT EXISTS carnet_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            carnet_id INTEGER REFERENCES carnets(id) ON DELETE CASCADE,
            target_carnet_id INTEGER REFERENCES carnets(id) ON DELETE SET NULL,
            position INTEGER NOT NULL DEFAULT 0,
            kind TEXT NOT NULL CHECK(kind IN ('link','photo','note','location','budget')),
            title TEXT DEFAULT '',
            body TEXT DEFAULT '',
            url TEXT DEFAULT '',
            photo_id INTEGER REFERENCES photos(id) ON DELETE SET NULL,
            address TEXT DEFAULT '',
            geo_lat REAL,
            geo_lng REAL,
            amount REAL,
            currency TEXT DEFAULT 'EUR',
            added_by INTEGER NOT NULL REFERENCES users(id),
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
        "CREATE INDEX IF NOT EXISTS idx_carnet_items ON carnet_items(carnet_id, position)",
        "CREATE INDEX IF NOT EXISTS idx_carnet_items_target ON carnet_items(target_carnet_id)",
        # ── v3.2 — Fil d'activité partagé (couple) ──────────────────────
        """CREATE TABLE IF NOT EXISTS activity_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            couple_id INTEGER NOT NULL REFERENCES couples(id) ON DELETE CASCADE,
            actor_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            kind TEXT NOT NULL,
            target_carnet_id INTEGER REFERENCES carnets(id) ON DELETE CASCADE,
            payload TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
        "CREATE INDEX IF NOT EXISTS idx_activity_couple ON activity_events(couple_id, created_at DESC)",
        """CREATE TABLE IF NOT EXISTS activity_seen (
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            couple_id INTEGER NOT NULL REFERENCES couples(id) ON DELETE CASCADE,
            last_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, couple_id)
        )""",
        # ── v5.1/v5.2 — la journee est une suite de BLOCS ───────────────
        # Un bloc = un mode + une heure optionnelle + des etapes ordonnees.
        # >= 2 etapes -> un trajet (trace, temps) ; 1 etape -> une halte
        # (le restaurant de midi). AUCUNE unicite sur item_id : une epingle
        # peut revenir dans le meme bloc (aller-retour A->B->A), dans deux
        # blocs, dans deux jours. L'identite d'une etape est sa POSITION.
        # carnet_items.planned_day reste une valeur DERIVEE = premier jour
        # d'apparition (couleur des epingles, album, retro-compat).
        """CREATE TABLE IF NOT EXISTS trajets (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            carnet_id  INTEGER NOT NULL REFERENCES carnets(id) ON DELETE CASCADE,
            day        INTEGER NOT NULL,
            ordre      INTEGER NOT NULL DEFAULT 0,
            mode       TEXT NOT NULL DEFAULT 'car',
            heure      TEXT NOT NULL DEFAULT ''
        )""",
        """CREATE TABLE IF NOT EXISTS trajet_steps (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            trajet_id  INTEGER NOT NULL REFERENCES trajets(id) ON DELETE CASCADE,
            position   INTEGER NOT NULL DEFAULT 0,
            item_id    INTEGER NOT NULL REFERENCES carnet_items(id) ON DELETE CASCADE
        )""",
        "CREATE INDEX IF NOT EXISTS idx_trajets_carnet ON trajets(carnet_id, day, ordre)",
        # v5.6 — on n'a pas toujours fait ce qu'on avait prevu. Un bloc peut
        # etre marque « pas fait » : il sort des temps, de l'album et du livre,
        # mais il RESTE (on se souvient de ce qu'on avait imagine), et le geste
        # se defait d'une touche.
        "ALTER TABLE trajets ADD COLUMN fait INTEGER NOT NULL DEFAULT 1",
        "CREATE INDEX IF NOT EXISTS idx_trajet_steps ON trajet_steps(trajet_id, position)",
        # ── v5.4 — reinitialisation de mot de passe par lien a usage unique
        # Le jeton n'est JAMAIS stocke en clair : la base ne garde que son
        # empreinte SHA-256. Il ne s'affiche qu'une fois, a l'admin qui le cree.
        """CREATE TABLE IF NOT EXISTS password_resets (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            token_hash TEXT NOT NULL UNIQUE,
            expires_at TIMESTAMP NOT NULL,
            used_at    TIMESTAMP,
            created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
        "CREATE INDEX IF NOT EXISTS idx_pwreset_user ON password_resets(user_id, expires_at)",
        # Horodatage du dernier changement de mot de passe : sert a couper les
        # sessions ouvertes AVANT le changement (un reset qui laisse la session
        # d'un intrus ouverte ne protege de rien).
        "ALTER TABLE users ADD COLUMN pw_changed_at TIMESTAMP",
        # ── v5.2 — cache des itineraires OSRM ───────────────────────────
        # Sans lui, chaque affichage de la reverie rejoue un appel par bloc
        # sur un service public en fair use (et la page attend le reseau).
        """CREATE TABLE IF NOT EXISTS route_cache (
            key        TEXT PRIMARY KEY,
            profile    TEXT NOT NULL DEFAULT 'car',
            duration_s INTEGER,
            distance_m INTEGER,
            legs       TEXT DEFAULT '',
            geometry   TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
        # ── v5.7 — chronologie fiable : origine de la date de prise
        # ('exif' | 'fichier' | 'manuel' | '' pour l'historique) + nom de
        # fichier d'origine (les photos passees par WhatsApp/Instagram sont
        # strippees de leur EXIF -> la date se devine dans le nom de fichier).
        # Regle R4 : tout fallback se VOIT a l'ecran (badge), jamais muet.
        "ALTER TABLE photos ADD COLUMN taken_at_source TEXT DEFAULT ''",
        "ALTER TABLE photos ADD COLUMN orig_filename TEXT DEFAULT ''",
        # ── v5.7 — taille intermediaire 1024px pour la photo hero d'une
        # scene (le thumb 400 est trop juste en pleine largeur, l'original
        # 2000 trop lourd). Generee a l'upload pour le neuf, paresseusement
        # pour l'existant (_ensure_mid) -> pas de migration lourde au boot.
        "ALTER TABLE photos ADD COLUMN mid_path TEXT DEFAULT ''",
        # ── v5.8 — epingles sur photo : note ancree a un point (x, y) de
        # l'image, coordonnees NORMALISEES 0-1 (independantes de la taille
        # d'affichage). Multi-auteur : couleur par membre dans la lightbox.
        """CREATE TABLE IF NOT EXISTS photo_notes (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            photo_id   INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
            x          REAL NOT NULL,
            y          REAL NOT NULL,
            texte      TEXT NOT NULL DEFAULT '',
            auteur_id  INTEGER REFERENCES users(id) ON DELETE SET NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
        "CREATE INDEX IF NOT EXISTS idx_photo_notes_photo ON photo_notes(photo_id)",
        # ── v5.9 — la date charniere du recit : le moment ou la conversation
        # s'arrete et ou l'album prend le relais sur la MEME timeline
        # (le seul moment monumental de la page Histoire).
        "ALTER TABLE couples ADD COLUMN date_charniere TEXT DEFAULT ''",
        "ALTER TABLE couples ADD COLUMN charniere_titre TEXT DEFAULT ''",
        # ── v5.10 — origine du lieu d'une photo ('exif' | 'manuel' | '')
        # meme logique de tracabilite que taken_at_source.
        "ALTER TABLE photos ADD COLUMN lieu_source TEXT DEFAULT ''",
        # ── v5.12 — les items (etapes, liens, notes...) se suppriment en
        # SOFT : au retour du sejour on retire ce qu'on n'a pas fait, et on
        # peut toujours le restaurer (retour d'Arthur du 2026-08-14).
        "ALTER TABLE carnet_items ADD COLUMN deleted_at TIMESTAMP",
        # ── v5.18 (audit livre §3) — la chronologie est l'UNIQUE ordre de
        # verite depuis que deplacer=re-dater (v5.13). Les carnets figes en
        # 'manual' par d'anciens drags imprimaient leurs positions perimees
        # (photos re-datees imprimees au mauvais endroit, faux bandeaux de
        # jour). Idempotent : ne touche que les carnets encore en manual.
        "UPDATE carnets SET sort_mode='chrono' WHERE sort_mode='manual'",
    ]
    for sql in migrations:
        try:
            conn.execute(sql)
        except Exception:
            pass
    conn.commit()

    # v5.1/v5.2 — backfill des plannings d'avant les blocs : un carnet qui a
    # des planned_day mais AUCUN bloc recoit un bloc 'car' par jour.
    # Hors de la boucle ci-dessus : celle-ci avale les exceptions, un echec de
    # backfill doit se VOIR dans les logs. Idempotent : on ne touche qu'aux
    # carnets sans aucun bloc, donc rejouable a chaque demarrage.
    try:
        orphelins = conn.execute("""
            SELECT DISTINCT ci.carnet_id FROM carnet_items ci
            WHERE ci.planned_day IS NOT NULL AND ci.planned_day >= 0
              AND ci.carnet_id IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM trajets t WHERE t.carnet_id = ci.carnet_id)
        """).fetchall()
        n_blocs = 0
        for (cid_,) in orphelins:
            jours = conn.execute(
                "SELECT DISTINCT planned_day FROM carnet_items "
                "WHERE carnet_id=? AND planned_day IS NOT NULL AND planned_day >= 0 "
                "ORDER BY planned_day", (cid_,)
            ).fetchall()
            for (jour,) in jours:
                tid = conn.execute(
                    "INSERT INTO trajets (carnet_id, day, ordre, mode, heure) "
                    "VALUES (?,?,0,'car','')", (cid_, jour)
                ).lastrowid
                conn.execute(
                    "INSERT INTO trajet_steps (trajet_id, position, item_id) "
                    "SELECT ?, position, id FROM carnet_items "
                    "WHERE carnet_id=? AND planned_day=? ORDER BY position",
                    (tid, cid_, jour)
                )
                n_blocs += 1
        if n_blocs:
            log.info("v5.2 backfill : %d bloc(s) recree(s) depuis planned_day", n_blocs)
        conn.commit()
    except Exception as e:
        log.error("v5.2 backfill blocs ECHEC : %s", e)

    conn.close()


def query(sql, params=(), one=False):
    conn = get_db()
    cur = conn.execute(sql, params)
    r = cur.fetchone() if one else cur.fetchall()
    conn.close()
    return r


def execute(sql, params=()):
    conn = get_db()
    cur = conn.execute(sql, params)
    conn.commit()
    lid = cur.lastrowid
    conn.close()
    return lid


# ── Auth helpers ──────────────────────────────────────────────────────
def hash_pw(pw: str) -> str:
    return bcrypt.hashpw(pw.encode('utf-8'), bcrypt.gensalt(rounds=12)).decode('utf-8')


def check_pw(pw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(pw.encode('utf-8'), hashed.encode('utf-8'))
    except Exception:
        return False


def current_user():
    uid = session.get('uid')
    if not uid:
        return None
    u = query("SELECT * FROM users WHERE id=?", (uid,), one=True)
    return dict(u) if u else None


def login_required(view):
    @wraps(view)
    def wrapper(*a, **kw):
        if not session.get('uid'):
            return redirect(url_for('login', next=request.path))
        return view(*a, **kw)
    return wrapper


def user_espaces(uid):
    """Liste tous les espaces dont l'user est membre."""
    if not uid:
        return []
    rows = query("""
        SELECT c.*, em.role, em.joined_at
        FROM couples c JOIN espace_members em ON em.espace_id = c.id
        WHERE em.user_id = ?
        ORDER BY em.joined_at ASC
    """, (uid,))
    return [dict(r) for r in rows]


def is_member(uid, eid):
    if not uid or not eid: return False
    r = query(
        "SELECT 1 FROM espace_members WHERE user_id=? AND espace_id=?",
        (uid, eid), one=True
    )
    return bool(r)


def current_espace_id():
    """Retourne l'espace courant. Migration douce : fallback sur couple_id legacy."""
    eid = session.get('espace_id')
    if eid: return eid
    # Fallback : si user a un couple_id (ancien modele), l'utilise comme espace
    leg = session.get('couple_id')
    if leg:
        session['espace_id'] = leg
        return leg
    return None


def current_espace():
    eid = current_espace_id()
    if not eid: return None
    r = query("SELECT * FROM couples WHERE id=?", (eid,), one=True)
    return dict(r) if r else None


def set_current_espace(eid):
    """Set l'espace courant si l'user est bien membre."""
    uid = session.get('uid')
    if not uid or not is_member(uid, eid):
        return False
    session['espace_id'] = int(eid)
    session['couple_id'] = int(eid)  # rétro-compat
    return True


def couple_required(view):
    """Decorator : require login + au moins un espace courant."""
    @wraps(view)
    def wrapper(*a, **kw):
        if not session.get('uid'):
            return redirect(url_for('login', next=request.path))
        if not current_espace_id():
            return redirect(url_for('onboarding_couple'))
        return view(*a, **kw)
    return wrapper

# Alias pour clarté
espace_required = couple_required


def admin_required(view):
    """Brief 06 §3.3 : reserve aux emails dans ADMIN_EMAILS."""
    @wraps(view)
    def wrapper(*a, **kw):
        if not session.get('uid'):
            return redirect(url_for('login', next=request.path))
        u = current_user()
        if not u or (u.get('email') or '').lower() not in ADMIN_EMAILS:
            abort(403)
        return view(*a, **kw)
    return wrapper


# ── v5.4 — reinitialisation de mot de passe ───────────────────────────
# Chemin retenu (2026-07-28) : pas d'email sur Carnet, donc pas de « mot de
# passe oublie » autonome. L'admin genere un LIEN A USAGE UNIQUE et le
# transmet (SMS, WhatsApp) ; la personne clique et choisit son mot de passe.
# Rien a retenir, rien a recopier — ca marche a tout age.
RESET_TTL_HEURES = 24
RESET_MAX_PAR_HEURE = 3        # demandes autonomes tolerees pour un meme compte

# Envoi d'emails via Resend (meme fournisseur qu'AqGK). Tant que la cle n'est
# pas posee, l'app ne fait PAS semblant : la page « mot de passe oublie » garde
# le chemin par l'admin, et le dit. Aucun email fantome, aucun faux succes.
RESEND_API_KEY = os.environ.get('RESEND_API_KEY', '')
MAIL_FROM = os.environ.get('MAIL_FROM', 'Notre Histoire <histoire@aqgk.fr>')


def mail_configure():
    return bool(RESEND_API_KEY)


def _envoyer_mail(destinataire, sujet, html):
    """Envoie un email. Retourne True seulement si Resend l'a accepte."""
    if not RESEND_API_KEY:
        log.warning("email non envoye a %s (%s) : RESEND_API_KEY absente",
                    destinataire, sujet)
        return False
    import json as _json
    import urllib.request as _url, urllib.error as _urlerr
    corps = _json.dumps({'from': MAIL_FROM, 'to': [destinataire],
                         'subject': sujet, 'html': html}).encode('utf-8')
    req = _url.Request('https://api.resend.com/emails', data=corps, method='POST',
                       headers={'Authorization': 'Bearer ' + RESEND_API_KEY,
                                'Content-Type': 'application/json',
                                'User-Agent': 'NotreHistoire/1.0'})
    try:
        with _url.urlopen(req, timeout=10) as resp:
            if resp.status in (200, 201):
                log.info("email envoye a %s (%s)", destinataire, sujet)
                return True
            log.error("Resend a repondu %s pour %s", resp.status, destinataire)
    except _urlerr.HTTPError as e:
        log.error("Resend HTTP %s pour %s : %s", e.code, destinataire,
                  e.read().decode('utf-8', 'replace')[:300])
    except Exception as e:
        log.error("Resend injoignable pour %s : %s", destinataire, e)
    return False


def _mail_reset_html(prenom, lien):
    """Le mail est court : une phrase, un bouton, une precaution."""
    return (
        '<!doctype html><html><head><meta charset="utf-8"></head>'
        '<body style="margin:0;padding:28px;background:#FBFBFA;'
        'font-family:Helvetica,Arial,sans-serif;color:#17181A">'
        '<div style="max-width:480px;margin:0 auto;background:#fff;'
        'border:1px solid rgba(23,24,26,0.08);border-radius:14px;padding:32px">'
        '<p style="margin:0 0 6px;font-size:11px;letter-spacing:.12em;'
        'text-transform:uppercase;color:#6C6E71">Notre Histoire</p>'
        '<h1 style="margin:0 0 16px;font-size:22px;font-weight:400;font-style:italic">'
        'Bonjour ' + (prenom or '') + ',</h1>'
        '<p style="margin:0 0 24px;font-size:15px;line-height:1.6;color:#3B3D40">'
        'Voici votre lien pour choisir un nouveau mot de passe. '
        'Touchez le bouton, écrivez votre mot de passe deux fois, et c\'est réglé.</p>'
        '<p style="margin:0 0 24px"><a href="' + lien + '" '
        'style="display:inline-block;background:#A8503D;color:#fff;'
        'text-decoration:none;padding:14px 26px;border-radius:999px;'
        'font-size:15px">Choisir mon mot de passe</a></p>'
        '<p style="margin:0;font-size:13px;line-height:1.6;color:#6C6E71">'
        'Ce lien ne fonctionne qu\'une seule fois et expire dans ' +
        str(RESET_TTL_HEURES) + ' heures. '
        'Si vous n\'avez rien demandé, ignorez ce message : votre mot de passe '
        'actuel reste valable.</p>'
        '</div></body></html>')


def _maintenant():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _reset_hash(token):
    """La base ne connait que l'empreinte. Un vol de base ne donne aucun lien."""
    return hashlib.sha256((token or '').encode('utf-8')).hexdigest()


def _reset_creer(user_id, par_uid=None):
    """Cree un lien a usage unique et renvoie le jeton EN CLAIR — la seule
    fois ou il existera ailleurs que dans la tete de celui qui le lit.
    Tout jeton encore valide de cette personne est annule au passage."""
    execute("UPDATE password_resets SET used_at=CURRENT_TIMESTAMP "
            "WHERE user_id=? AND used_at IS NULL", (user_id,))
    token = secrets.token_urlsafe(32)
    expire = (datetime.now() + timedelta(hours=RESET_TTL_HEURES)
              ).strftime('%Y-%m-%d %H:%M:%S')
    execute("INSERT INTO password_resets (user_id, token_hash, expires_at, created_by) "
            "VALUES (?,?,?,?)", (user_id, _reset_hash(token), expire, par_uid))
    return token


def _reset_lire(token):
    """Retourne l'utilisateur si le jeton est bon, None sinon.
    Inconnu, expire, deja servi, compte supprime : MEME reponse. On ne dit
    jamais laquelle des quatre — sinon le lien devient un detecteur de comptes."""
    if not token or len(token) < 20:
        return None
    r = query("SELECT * FROM password_resets WHERE token_hash=?",
              (_reset_hash(token),), one=True)
    if not r or r['used_at']:
        return None
    try:
        if datetime.strptime(str(r['expires_at'])[:19], '%Y-%m-%d %H:%M:%S') < datetime.now():
            return None
    except ValueError:
        return None
    u = query("SELECT * FROM users WHERE id=? AND deleted_at IS NULL",
              (r['user_id'],), one=True)
    return dict(u) if u else None


def _reset_consommer(token, user_id, nouveau_mdp):
    """Pose le nouveau mot de passe et brule le jeton, dans la meme transaction.
    Marque aussi pw_changed_at : les sessions ouvertes avant tombent."""
    conn = get_db()
    try:
        cur = conn.execute(
            "UPDATE password_resets SET used_at=CURRENT_TIMESTAMP "
            "WHERE token_hash=? AND used_at IS NULL", (_reset_hash(token),))
        if not cur.rowcount:
            conn.rollback()
            return False              # deja servi entre-temps : on ne rejoue pas
        conn.execute("UPDATE users SET password_hash=?, pw_changed_at=CURRENT_TIMESTAMP "
                     "WHERE id=?", (hash_pw(nouveau_mdp), user_id))
        conn.commit()
        return True
    finally:
        conn.close()


def csrf_token():
    """Genere et stocke un token CSRF par session (rotation manuelle si besoin)."""
    if '_csrf' not in session:
        session['_csrf'] = secrets.token_urlsafe(32)
    return session['_csrf']


def csrf_check():
    """Verifie le token CSRF sur les POST sensibles. Renvoie True/False."""
    sent = request.form.get('_csrf') or request.headers.get('X-CSRF-Token')
    return sent and sent == session.get('_csrf')


@app.context_processor
def inject_globals():
    """Variables disponibles dans tous les templates.

    Defensif : aucune exception ne doit faire echouer le rendu d'une page,
    sinon on tombe sur un Internal Server Error generique (Brief 08 §4).
    """
    try:
        u = current_user()
    except Exception:
        u = None
    try:
        espaces = user_espaces(u['id']) if u else []
    except Exception:
        espaces = []
    try:
        esp = current_espace()
    except Exception:
        esp = None
    nb_souhaits = 0
    try:
        eid = current_espace_id()
    except Exception:
        eid = None
    if eid:
        try:
            r = query("SELECT COUNT(*) AS n FROM carnets WHERE couple_id=? "
                      "AND type='souhait' AND deleted_at IS NULL", (eid,), one=True)
            nb_souhaits = r['n'] if r else 0
        except Exception:
            nb_souhaits = 0
    try:
        is_admin = bool(u and (u.get('email') or '').lower() in ADMIN_EMAILS)
    except Exception:
        is_admin = False
    nb_activity_unseen = 0
    if u and eid:
        try:
            nb_activity_unseen = _count_unseen_activity(u['id'], eid)
        except Exception:
            nb_activity_unseen = 0
    try:
        accent = (esp.get('accent') if esp else 'terracotta') or 'terracotta'
    except Exception:
        accent = 'terracotta'
    return {
        'current_user': u,
        'current_espace': esp,
        'current_accent': accent,
        'user_espaces': espaces,
        'nb_souhaits': nb_souhaits,
        'nb_activity_unseen': nb_activity_unseen,
        'is_admin': is_admin,
        'admin_emails': ADMIN_EMAILS,
        'csrf_token': csrf_token,
        'app_version': APP_VERSION,
        'asset_v': ASSET_V,
        'accents': ACCENTS,
    }


# ── QR helper (SVG inline) ────────────────────────────────────────────
def qr_svg(data: str) -> str:
    img = qrcode.make(data, image_factory=qrsvg.SvgPathImage, box_size=10, border=1)
    buf = io.BytesIO()
    img.save(buf)
    return buf.getvalue().decode('utf-8')


# ── Routes : public ──────────────────────────────────────────────────
@app.route('/healthz')
def healthz():
    return jsonify({'ok': True, 'version': APP_VERSION})


CARNET_TYPES = [
    ('voyage',     'Voyage'),
    ('restaurant', 'Restaurant'),
    ('sortie',     'Sortie'),
    ('autre',      'Autre'),
]

# Categories dispo pour un carnet de souhait (le type du futur voyage)
SOUHAIT_KINDS = [
    ('voyage',     'Voyage'),
    ('restaurant', 'Restaurant'),
    ('sortie',     'Sortie'),
    ('autre',      'Autre'),
]

# v4.5 : types d'epingle sur la carte des reveries (choix au moment d'epingler)
PIN_KINDS = [
    ('dormir', '🛏', 'Dormir'),
    ('manger', '🍽', 'Manger'),
    ('rando',  '🥾', 'Rando'),
    ('plage',  '🏖', 'Plage'),
    ('visite', '🏛', 'Visite'),
    ('autre',  '📍', 'Autre'),
]

ITEM_KINDS = [
    ('link',     'Lien'),
    ('photo',    'Photo'),
    ('note',     'Note'),
    ('location', 'Lieu'),
    ('budget',   'Budget'),
]


@app.route('/')
def home():
    """Accueil : liste verticale des carnets de l'espace courant, filtre par type."""
    if not session.get('uid'):
        return redirect(url_for('login'))
    cid = current_espace_id()
    if not cid:
        # Brief 08 §2 : pas d'espace par defaut. Si l'user a des espaces,
        # il choisit explicitement ; sinon onboarding.
        esps = user_espaces(session['uid'])
        if esps:
            return redirect(url_for('espace_choisir'))
        return redirect(url_for('onboarding_couple'))
    type_filter = request.args.get('type') or ''
    if type_filter and type_filter not in dict(CARNET_TYPES):
        type_filter = ''
    # Exclure les carnets de souhait (page dediee /souhaits)
    if type_filter:
        rows = query(
            "SELECT * FROM carnets WHERE couple_id=? AND type=? AND type != 'souhait' "
            "AND deleted_at IS NULL ORDER BY COALESCE(date_start, created_at) DESC, id DESC",
            (cid, type_filter)
        )
    else:
        rows = query(
            "SELECT * FROM carnets WHERE couple_id=? AND type != 'souhait' "
            "AND deleted_at IS NULL ORDER BY COALESCE(date_start, created_at) DESC, id DESC",
            (cid,)
        )
    # Compteur de souhaits actifs (pour le badge nav)
    nb_row = query(
        "SELECT COUNT(*) AS n FROM carnets WHERE couple_id=? AND type='souhait' AND deleted_at IS NULL",
        (cid,), one=True
    )
    nb_souhaits = (nb_row['n'] if nb_row else 0)
    # v5.11 : corbeille — les carnets supprimes se restaurent d'ici
    corbeille = query(
        "SELECT id, title, type, deleted_at FROM carnets "
        "WHERE couple_id=? AND type != 'souhait' AND deleted_at IS NOT NULL "
        "ORDER BY deleted_at DESC", (cid,)
    )
    return render_template(
        'index.html',
        carnets=[dict(r) for r in rows],
        types=CARNET_TYPES,
        type_filter=type_filter,
        nb_souhaits=nb_souhaits,
        corbeille=[dict(r) for r in corbeille],
    )


@app.route('/souhaits')
@couple_required
def souhaits_index():
    """Page propre des carnets de souhait, avec chips de filtre par categorie."""
    cid = current_espace_id()
    kind_filter = request.args.get('kind') or ''
    if kind_filter and kind_filter not in dict(SOUHAIT_KINDS):
        kind_filter = ''
    if kind_filter:
        rows = query(
            "SELECT * FROM carnets WHERE couple_id=? AND type='souhait' "
            "AND COALESCE(souhait_kind,'voyage')=? AND deleted_at IS NULL "
            "ORDER BY updated_at DESC, id DESC",
            (cid, kind_filter)
        )
    else:
        rows = query(
            "SELECT * FROM carnets WHERE couple_id=? AND type='souhait' "
            "AND deleted_at IS NULL ORDER BY updated_at DESC, id DESC",
            (cid,)
        )
    # Compter items pour chaque souhait
    souhaits = []
    for r in rows:
        r = dict(r)
        cnt = query(
            "SELECT COUNT(*) AS n FROM carnet_items WHERE carnet_id=? AND target_carnet_id IS NULL AND deleted_at IS NULL",
            (r['id'],), one=True
        )
        r['nb_items'] = cnt['n'] if cnt else 0
        souhaits.append(r)
    # v5.11 : corbeille des reveries supprimees (restaurables)
    corbeille = query(
        "SELECT id, title, souhait_kind, deleted_at FROM carnets "
        "WHERE couple_id=? AND type='souhait' AND deleted_at IS NOT NULL "
        "ORDER BY deleted_at DESC", (current_espace_id(),)
    )
    return render_template(
        'souhaits.html',
        souhaits=souhaits,
        kinds=SOUHAIT_KINDS,
        kind_filter=kind_filter,
        corbeille=[dict(r) for r in corbeille],
    )


@app.route('/souhait/nouveau', methods=['GET', 'POST'])
@couple_required
def souhait_nouveau():
    """Creation d'un carnet de souhait avec sa categorie (kind)."""
    if request.method == 'POST':
        if not csrf_check():
            flash("Session expiree.", "err")
            return redirect(url_for('souhait_nouveau'))
        title = (request.form.get('title') or '').strip()
        kind = (request.form.get('souhait_kind') or 'voyage').strip()
        if kind not in dict(SOUHAIT_KINDS):
            kind = 'voyage'
        if not title:
            flash("Donne un titre au souhait.", "err")
            return render_template('souhait_form.html', kinds=SOUHAIT_KINDS,
                souhait={'title': '', 'souhait_kind': kind})
        cid = execute(
            "INSERT INTO carnets (couple_id, title, type, souhait_kind, status, created_by) "
            "VALUES (?,?,?,?,?,?)",
            (current_espace_id(), title, 'souhait', kind, 'active', session['uid'])
        )
        _log_activity(current_espace_id(), session['uid'], 'souhait_created',
                      target_carnet_id=cid, payload={'title': title, 'kind': kind})
        return redirect(url_for('carnet_souhait_view', cid_carnet=cid))
    return render_template('souhait_form.html', kinds=SOUHAIT_KINDS, souhait=None)


# ── Routes : carnets ─────────────────────────────────────────────────
def _get_carnet_or_404(cid_carnet):
    """Recupere un carnet en verifiant qu'il appartient a l'espace courant."""
    c = query("SELECT * FROM carnets WHERE id=? AND deleted_at IS NULL", (cid_carnet,), one=True)
    if not c or c['couple_id'] != current_espace_id():
        abort(404)
    return dict(c)


def _parse_carnet_form(form):
    """Extrait + valide les champs du formulaire carnet. Renvoie (data, errors)."""
    title = (form.get('title') or '').strip()
    type_ = (form.get('type') or 'voyage').strip()
    location = (form.get('location') or '').strip()
    date_start = (form.get('date_start') or '').strip() or None
    date_end = (form.get('date_end') or '').strip() or None
    errors = []
    if not title:
        errors.append("Donne un titre au carnet.")
    elif len(title) > 80:
        errors.append("Titre : 80 caracteres maximum.")
    if type_ not in dict(CARNET_TYPES):
        type_ = 'voyage'
    if date_start and date_end and date_end < date_start:
        errors.append("La date de fin est avant la date de debut.")
    return {
        'title': title, 'type': type_, 'location': location,
        'date_start': date_start, 'date_end': date_end,
    }, errors


@app.route('/carnet/nouveau', methods=['GET', 'POST'])
@couple_required
def carnet_nouveau():
    if request.method == 'POST':
        if not csrf_check():
            flash("Session expiree.", "err")
            return redirect(url_for('carnet_nouveau'))
        data, errors = _parse_carnet_form(request.form)
        if errors:
            for e in errors:
                flash(e, "err")
            return render_template('carnet_form.html', mode='nouveau', carnet=data, types=CARNET_TYPES)
        cid = execute(
            "INSERT INTO carnets (couple_id, title, type, location, date_start, date_end, "
            "status, created_by) VALUES (?,?,?,?,?,?,?,?)",
            (current_espace_id(), data['title'], data['type'], data['location'],
             data['date_start'], data['date_end'], 'active', session['uid'])
        )
        _log_activity(current_espace_id(), session['uid'], 'carnet_created',
                      target_carnet_id=cid,
                      payload={'title': data['title'], 'type': data['type']})
        return redirect(url_for('carnet_view', cid_carnet=cid))
    return render_template('carnet_form.html', mode='nouveau', carnet=None, types=CARNET_TYPES)


@app.route('/carnet/<int:cid_carnet>')
@couple_required
def carnet_view(cid_carnet):
    """Brief 06 §4.2 : page intermediaire supprimee, redirige direct vers album/reverie."""
    c = _get_carnet_or_404(cid_carnet)
    if c['type'] == 'souhait':
        return redirect(url_for('carnet_souhait_view', cid_carnet=cid_carnet))
    return redirect(url_for('carnet_album', cid_carnet=cid_carnet))


# ══════════════════════════════════════════════════════════════════════
#                    v1.4 — CARNETS DE SOUHAIT
# ══════════════════════════════════════════════════════════════════════

def _carnet_items(carnet_id):
    rows = query("""
        SELECT ci.*, p.thumb_path AS photo_thumb, p.file_path AS photo_path,
               p.gps_lat AS photo_gps_lat, p.gps_lng AS photo_gps_lng,
               p.address_full AS photo_address_full,
               p.country AS photo_country, p.state AS photo_state,
               p.city_name AS photo_city, p.road AS photo_road,
               u.display_name AS added_by_name
        FROM carnet_items ci
        LEFT JOIN photos p ON p.id = ci.photo_id
        LEFT JOIN users u ON u.id = ci.added_by
        WHERE ci.carnet_id = ? AND ci.target_carnet_id IS NULL
          AND ci.deleted_at IS NULL
        ORDER BY ci.position ASC, ci.id ASC
    """, (carnet_id,))
    return [dict(r) for r in rows]


def _next_item_position(carnet_id):
    r = query(
        "SELECT COALESCE(MAX(position), -1) + 1 AS next FROM carnet_items WHERE carnet_id=?",
        (carnet_id,), one=True
    )
    return r['next'] if r else 0


TRAJET_MODES = ('car', 'foot', 'bike')


def _trajet_blocs(carnet_id, nb_days=None):
    """v5.2 : la journee comme suite de BLOCS.
    Retourne [ [ {mode, heure, steps:[item_id, ...]}, ... ], ... ] indexe par jour.
    Un bloc a >=2 etapes est un trajet (trace + temps), un bloc a 1 etape est
    une halte (le restaurant de midi). Une epingle peut revenir dans le meme
    bloc (A->B->A), dans deux blocs, dans deux jours.
    Repli explicite (bruyant) : si les blocs sont illisibles ou absents pour ce
    carnet, on derive un bloc par jour depuis planned_day."""
    try:
        rows = query("""
            SELECT t.id AS tid, t.day, t.ordre, t.mode, t.heure, t.fait,
                   s.item_id, s.position
            FROM trajets t LEFT JOIN trajet_steps s ON s.trajet_id = t.id
            WHERE t.carnet_id = ?
            ORDER BY t.day ASC, t.ordre ASC, t.id ASC, s.position ASC, s.id ASC
        """, (carnet_id,))
    except Exception as e:
        log.error("blocs illisibles (carnet %s) : %s — repli sur planned_day",
                  carnet_id, e)
        rows = []
    days, blocs = [], {}
    for r in rows:
        k = r['day']
        if k is None or k < 0 or k > 30:
            continue
        while len(days) <= k:
            days.append([])
        b = blocs.get(r['tid'])
        if b is None:
            b = {'id': r['tid'], 'mode': r['mode'] or 'car',
                 'heure': r['heure'] or '',
                 'fait': 1 if (r['fait'] is None or r['fait']) else 0,
                 'steps': []}
            blocs[r['tid']] = b
            days[k].append(b)
        if r['item_id'] is not None:
            b['steps'].append(r['item_id'])
    if not rows:
        # planning d'avant les blocs : un bloc 'car' par jour
        legacy = query("SELECT planned_day AS day, id AS item_id FROM carnet_items "
                       "WHERE carnet_id=? AND planned_day IS NOT NULL "
                       "AND deleted_at IS NULL "
                       "ORDER BY planned_day ASC, position ASC", (carnet_id,))
        for r in legacy:
            k = r['day']
            if k is None or k < 0 or k > 30:
                continue
            while len(days) <= k:
                days.append([])
            if not days[k]:
                days[k].append({'id': None, 'mode': 'car', 'heure': '',
                                'fait': 1, 'steps': []})
            days[k][0]['steps'].append(r['item_id'])
    if nb_days is not None:
        while len(days) < nb_days:
            days.append([])
        days = days[:nb_days]
    return days


def _trajet_save(carnet_id, days):
    """v5.2 : reecrit le planning du carnet.
    days = [ [ {mode, heure, steps:[item_id, ...]}, ... ], ... ] par jour.
    Tolere l'ANCIEN format (days = [[item_id, ...], ...], un client reste
    ouvert sur la page d'avant) : chaque jour devient alors un bloc unique.
    Seuls les items DU carnet sont acceptes (cloisonnement D1).
    planned_day / position restent des valeurs DERIVEES (premier jour
    d'apparition) : couleur des epingles, album, retro-compat."""
    a_moi = {r['id'] for r in query(
        "SELECT id FROM carnet_items WHERE carnet_id=?", (carnet_id,))}
    # v5.18 (P0 audit UX) : le format PLAT vient du planning de l'ALBUM
    # (vp-days). Avant, chaque jour devenait UN bloc 'car' sans heure ->
    # un simple drag dans l'album ECRASAIT les modes, heures, haltes et
    # marquages 'pas fait' prepares en reverie (perte silencieuse, R4).
    # Maintenant : FUSION — la structure de blocs existante est conservee,
    # seuls les changements d'appartenance aux jours s'appliquent.
    est_plat = any(j and not isinstance(j[0], dict) for j in days if j)
    if est_plat:
        existants = _trajet_blocs(carnet_id)
        fusion = []
        for k, jour in enumerate(days[:31]):
            ids = [int(x) for x in (jour or []) if str(x).isdigit()]
            blocs_jour = []
            couverts = set()
            for bloc in (existants[k] if k < len(existants) else []):
                steps = [i for i in (bloc.get('steps') or []) if i in ids]
                if steps:
                    blocs_jour.append({'mode': bloc.get('mode') or 'car',
                                       'heure': bloc.get('heure') or '',
                                       'fait': bloc.get('fait', 1),
                                       'steps': steps})
                    couverts.update(steps)
            nouveaux = [i for i in ids if i not in couverts]
            if nouveaux:
                blocs_jour.append({'mode': 'car', 'heure': '', 'steps': nouveaux})
            fusion.append(blocs_jour)
        days = fusion
    conn = get_db()
    try:
        conn.execute("DELETE FROM trajet_steps WHERE trajet_id IN "
                     "(SELECT id FROM trajets WHERE carnet_id=?)", (carnet_id,))
        conn.execute("DELETE FROM trajets WHERE carnet_id=?", (carnet_id,))
        conn.execute("UPDATE carnet_items SET planned_day=NULL WHERE carnet_id=?",
                     (carnet_id,))
        pos, vus = 0, set()
        for k, jour in enumerate(days[:31]):
            jour = jour or []
            # ANCIEN format (client reste ouvert sur la page d'avant la v5.2) :
            # la journee est une liste plate d'item_id -> elle devient UN bloc.
            if jour and not isinstance(jour[0], dict):
                jour = [{'mode': 'car', 'heure': '', 'steps': list(jour)}]
            for ordre, bloc in enumerate(jour):
                if not isinstance(bloc, dict):
                    continue
                mode = bloc.get('mode') if bloc.get('mode') in TRAJET_MODES else 'car'
                heure = str(bloc.get('heure') or '')[:5]
                fait = 0 if str(bloc.get('fait', 1)) in ('0', 'False', 'false') else 1
                steps = [int(x) for x in (bloc.get('steps') or [])
                         if str(x).isdigit() and int(x) in a_moi]
                if not steps:
                    continue                    # un bloc vide ne se garde pas
                tid = conn.execute(
                    "INSERT INTO trajets (carnet_id, day, ordre, mode, heure, fait) "
                    "VALUES (?,?,?,?,?,?)", (carnet_id, k, ordre, mode, heure, fait)
                ).lastrowid
                for p, iid in enumerate(steps):
                    conn.execute(
                        "INSERT INTO trajet_steps (trajet_id, position, item_id) "
                        "VALUES (?,?,?)", (tid, p, iid))
                    if iid not in vus:
                        vus.add(iid)
                        conn.execute("UPDATE carnet_items SET planned_day=?, position=? "
                                     "WHERE id=? AND carnet_id=?",
                                     (k, pos, iid, carnet_id))
                        pos += 1
        conn.commit()
    finally:
        conn.close()


def _trajet_days(carnet_id, nb_days=None):
    """Vue aplatie des blocs : [[item_id, ...], ...] par jour, sans doublon.
    Ce que consomment l'album et la transformation, qui se moquent des blocs."""
    days = []
    for jour in _trajet_blocs(carnet_id, nb_days):
        plat = []
        for bloc in jour:
            if not bloc.get('fait', 1):
                continue          # pas fait : hors album, hors livre
            for iid in bloc['steps']:
                if iid not in plat:
                    plat.append(iid)
        days.append(plat)
    return days


@app.route('/carnet/<int:cid_carnet>/souhait')
@couple_required
def carnet_souhait_view(cid_carnet):
    c = _get_carnet_or_404(cid_carnet)
    items = _carnet_items(cid_carnet)
    # v4.1 : backfill best-effort — geocoder les lieux qui n'ont qu'une adresse
    # (max 2 par affichage pour respecter la policy Nominatim 1 req/s)
    backfilled = 0
    for it in items:
        if backfilled >= 2:
            break
        if (it.get('kind') == 'location' and it.get('address')
                and it.get('geo_lat') is None):
            res = _forward_geocode(it['address'], limit=1)
            if res:
                execute("UPDATE carnet_items SET geo_lat=?, geo_lng=? WHERE id=?",
                        (res[0]['lat'], res[0]['lng'], it['id']))
                it['geo_lat'], it['geo_lng'] = res[0]['lat'], res[0]['lng']
            backfilled += 1
    # Voyages issus de cette reverie
    voyages = query(
        "SELECT id, title, status, created_at FROM carnets "
        "WHERE parent_souhait_id=? AND deleted_at IS NULL ORDER BY created_at DESC",
        (cid_carnet,)
    )
    # v5.2 : la journee est une suite de blocs (trajets + haltes)
    days_blocs = _trajet_blocs(cid_carnet)
    days_of = {}
    for k, jour in enumerate(days_blocs):
        for bloc in jour:
            for iid in bloc['steps']:
                if k not in days_of.setdefault(iid, []):
                    days_of[iid].append(k)
    # Brief 07 : carte des lieux de la reverie (items location + photos avec GPS)
    geo_items = []
    for it in items:
        if it.get('kind') == 'location' and it.get('geo_lat') is not None and it.get('geo_lng') is not None:
            pdays = days_of.get(it['id']) or []
            geo_items.append({
                'item_id': it['id'],
                'lat': it['geo_lat'], 'lng': it['geo_lng'],
                'kind': 'location',
                'pin_kind': it.get('pin_kind') or '',
                'planned_day': pdays[0] if pdays else None,
                'planned_days': pdays,
                'title': it.get('title') or it.get('address') or 'Lieu',
                'address': it.get('address') or '',
            })
        elif it.get('kind') == 'photo' and it.get('photo_gps_lat') is not None and it.get('photo_gps_lng') is not None:
            geo_items.append({
                'item_id': it['id'],
                'lat': it['photo_gps_lat'], 'lng': it['photo_gps_lng'],
                'kind': 'photo',
                'title': it.get('title') or 'Photo',
                'thumb': url_for('serve_upload', filename=it['photo_thumb']) if it.get('photo_thumb') else None,
            })
    # v4.5 : notes rattachees a une epingle (regroupement)
    children_by_parent = {}
    for it in items:
        if it.get('parent_item_id'):
            children_by_parent.setdefault(it['parent_item_id'], []).append(it)
    # v4.7 : version legere pour le popup de la carte
    children_slim = {
        str(pid): [{'kind': ch.get('kind'), 'title': ch.get('title') or '',
                    'url': ch.get('url') or ''} for ch in lst[:6]]
        for pid, lst in children_by_parent.items()
    }
    # v5.12 : items supprimes de la reverie (restaurables)
    items_corbeille = [dict(r) for r in query("""
        SELECT id, kind, title, address, deleted_at FROM carnet_items
        WHERE carnet_id=? AND target_carnet_id IS NULL AND deleted_at IS NOT NULL
        ORDER BY deleted_at DESC
    """, (cid_carnet,))]
    return render_template('carnet_souhait.html', carnet=c, items=items,
        voyages=[dict(v) for v in voyages], item_kinds=ITEM_KINDS,
        pin_kinds=PIN_KINDS, children_by_parent=children_by_parent,
        children_slim=children_slim,
        days_blocs=days_blocs,
        items_corbeille=items_corbeille,
        geo_items=geo_items)


@app.route('/carnet/<int:cid_carnet>/item', methods=['POST'])
@couple_required
def carnet_add_item(cid_carnet):
    c = _get_carnet_or_404(cid_carnet)
    if not csrf_check():
        return jsonify({'ok': False, 'error': 'CSRF'}), 403
    kind = (request.form.get('kind') or '').strip()
    if kind not in dict(ITEM_KINDS):
        return jsonify({'ok': False, 'error': 'Type item invalide'}), 400
    title = (request.form.get('title') or '').strip()
    body = (request.form.get('body') or '').strip()
    url_v = (request.form.get('url') or '').strip()
    address = (request.form.get('address') or '').strip()
    amount = _safe_float(request.form.get('amount'))
    currency = (request.form.get('currency') or 'EUR').strip()[:3].upper() or 'EUR'
    photo_id = None
    # Photo : upload optionnel
    f = request.files.get('photo')
    if f and f.filename:
        try:
            data = _save_uploaded_photo(f, c['couple_id'])
            ct = request.form.get('taken_at') or ''
            if ct and ct != 'null': data['taken_at'] = ct
            gps_lat = _safe_float(request.form.get('gps_lat'))
            gps_lng = _safe_float(request.form.get('gps_lng'))
            # v1.2.4 — reinjection EXIF
            _inject_exif_to_jpeg(os.path.join(UPLOAD_DIR, data['file_path']),
                                 data.get('taken_at'), gps_lat, gps_lng)
            _inject_exif_to_jpeg(os.path.join(UPLOAD_DIR, data['thumb_path']),
                                 data.get('taken_at'), gps_lat, gps_lng)
            photo_id = execute(
                "INSERT INTO photos (couple_id, file_path, thumb_path, width, height, "
                "taken_at, gps_lat, gps_lng, added_by) VALUES (?,?,?,?,?,?,?,?,?)",
                (c['couple_id'], data['file_path'], data['thumb_path'],
                 data['width'], data['height'], data['taken_at'],
                 gps_lat, gps_lng, session['uid'])
            )
            _enrich_photo_geo(photo_id, gps_lat, gps_lng)
        except Exception as e:
            log.error("upload item photo: %s", e)
            return jsonify({'ok': False, 'error': 'Photo : ' + str(e)}), 500
    pos = _next_item_position(cid_carnet)
    # v4.1 : coordonnees posees depuis la carte des reveries
    item_lat = _safe_float(request.form.get('geo_lat'))
    item_lng = _safe_float(request.form.get('geo_lng'))
    # v4.5 : type d'epingle + rattachement a une epingle parente
    pin_kind = (request.form.get('pin_kind') or '').strip()
    if pin_kind and pin_kind not in {k for k, _, _ in PIN_KINDS}:
        pin_kind = 'autre'
    parent_id = None
    raw_parent = request.form.get('parent_item_id')
    if raw_parent and str(raw_parent).isdigit():
        par = query("SELECT id FROM carnet_items WHERE id=? AND carnet_id=?",
                    (int(raw_parent), cid_carnet), one=True)
        if par:
            parent_id = par['id']
    iid = execute(
        "INSERT INTO carnet_items (carnet_id, position, kind, title, body, url, "
        "photo_id, address, geo_lat, geo_lng, amount, currency, pin_kind, "
        "parent_item_id, added_by) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (cid_carnet, pos, kind, title, body, url_v, photo_id, address,
         item_lat, item_lng, amount, currency, pin_kind, parent_id, session['uid'])
    )
    return jsonify({'ok': True, 'item_id': iid, 'geo_lat': item_lat, 'geo_lng': item_lng})


@app.route('/item/<int:item_id>/geo', methods=['POST'])
@couple_required
def item_update_geo(item_id):
    """v4.1 : deplace l'epingle d'un item (drag sur la carte des reveries)."""
    if not csrf_check():
        return jsonify({'ok': False, 'error': 'CSRF'}), 403
    item = query("SELECT ci.id, c.couple_id FROM carnet_items ci "
                 "JOIN carnets c ON c.id=ci.carnet_id WHERE ci.id=?",
                 (item_id,), one=True)
    if not item or item['couple_id'] != current_espace_id():
        return jsonify({'ok': False, 'error': '404'}), 404
    lat = _safe_float(request.form.get('lat'))
    lng = _safe_float(request.form.get('lng'))
    if lat is None or lng is None:
        return jsonify({'ok': False, 'error': 'Coordonnees invalides'}), 400
    execute("UPDATE carnet_items SET geo_lat=?, geo_lng=? WHERE id=?",
            (lat, lng, item_id))
    return jsonify({'ok': True})


@app.route('/item/<int:item_id>/titre', methods=['POST'])
@couple_required
def item_set_titre(item_id):
    """v5.18 — Renomme une epingle/etape (verbatim Arthur : « je ne peux
    changer de nom »). Calquee sur item_set_pin_kind."""
    if not csrf_check():
        return jsonify({'ok': False, 'error': 'CSRF'}), 403
    item = query("SELECT ci.id, c.couple_id FROM carnet_items ci "
                 "JOIN carnets c ON c.id=ci.carnet_id WHERE ci.id=?",
                 (item_id,), one=True)
    if not item or item['couple_id'] != current_espace_id():
        return jsonify({'ok': False, 'error': '404'}), 404
    titre = (request.form.get('titre') or '').strip()[:120]
    if not titre:
        return jsonify({'ok': False, 'error': 'titre_vide'}), 400
    execute("UPDATE carnet_items SET title=? WHERE id=?", (titre, item_id))
    return jsonify({'ok': True, 'titre': titre})


@app.route('/item/<int:item_id>/pin_kind', methods=['POST'])
@couple_required
def item_set_pin_kind(item_id):
    """v4.5 : change le type d'une epingle (dormir, manger, rando...)."""
    if not csrf_check():
        return jsonify({'ok': False, 'error': 'CSRF'}), 403
    item = query("SELECT ci.id, c.couple_id FROM carnet_items ci "
                 "JOIN carnets c ON c.id=ci.carnet_id WHERE ci.id=?",
                 (item_id,), one=True)
    if not item or item['couple_id'] != current_espace_id():
        return jsonify({'ok': False, 'error': '404'}), 404
    kind = (request.form.get('pin_kind') or '').strip()
    if kind and kind not in {k for k, _, _ in PIN_KINDS}:
        return jsonify({'ok': False, 'error': 'Type inconnu'}), 400
    execute("UPDATE carnet_items SET pin_kind=? WHERE id=?", (kind, item_id))
    return jsonify({'ok': True, 'pin_kind': kind})


@app.route('/item/<int:item_id>/parent', methods=['POST'])
@couple_required
def item_set_parent(item_id):
    """v4.5 : rattache (ou detache) une note/lien/photo a une epingle."""
    if not csrf_check():
        return jsonify({'ok': False, 'error': 'CSRF'}), 403
    item = query("SELECT ci.*, c.couple_id AS cpl FROM carnet_items ci "
                 "JOIN carnets c ON c.id=ci.carnet_id WHERE ci.id=?",
                 (item_id,), one=True)
    if not item or item['cpl'] != current_espace_id():
        return jsonify({'ok': False, 'error': '404'}), 404
    raw = (request.form.get('parent_item_id') or '').strip()
    if not raw:
        execute("UPDATE carnet_items SET parent_item_id=NULL WHERE id=?", (item_id,))
        return jsonify({'ok': True, 'parent_item_id': None})
    if not raw.isdigit() or int(raw) == item_id:
        return jsonify({'ok': False, 'error': 'Cible invalide'}), 400
    target = query("SELECT id FROM carnet_items WHERE id=? AND carnet_id=?",
                   (int(raw), item['carnet_id']), one=True)
    if not target:
        return jsonify({'ok': False, 'error': 'Epingle introuvable'}), 404
    execute("UPDATE carnet_items SET parent_item_id=? WHERE id=?",
            (target['id'], item_id))
    return jsonify({'ok': True, 'parent_item_id': target['id']})


@app.route('/carnet/<int:cid_carnet>/planning', methods=['POST'])
@couple_required
def carnet_planning_save(cid_carnet):
    """v4.5 : sauvegarde le pre-planning de la reverie.
    JSON : {date_start, date_end, days: [[item_id,...], ...]} —
    days[k] = etapes du jour k dans l'ordre. Les items absents -> planned_day NULL.
    v5.1 : un meme item_id peut figurer dans PLUSIEURS jours (nuit d'etape) ;
    le trajet est ecrit dans trajet_steps, planned_day en devient la valeur
    derivee (premier jour d'apparition).
    Modifiable a volonte jusqu'au basculement en voyage."""
    c = _get_carnet_or_404(cid_carnet)
    # v4.6 : le planning se met a jour aussi sur un VOYAGE (prevu -> reel,
    # ajustable jusqu'a la livraison de l'album)
    if not csrf_check():
        return jsonify({'ok': False, 'error': 'CSRF'}), 403
    data = request.get_json(silent=True) or {}
    ds = (data.get('date_start') or '').strip() or None
    de = (data.get('date_end') or '').strip() or None
    if ds and de and de < ds:
        return jsonify({'ok': False, 'error': 'Dates inversees'}), 400
    days = data.get('days') or []
    execute("UPDATE carnets SET date_start=?, date_end=? WHERE id=?",
            (ds, de, cid_carnet))
    _trajet_save(cid_carnet, days)
    return jsonify({'ok': True})


@app.route('/item/<int:item_id>/supprimer', methods=['POST'])
@couple_required
def item_supprimer(item_id):
    if not csrf_check():
        return jsonify({'ok': False, 'error': 'CSRF'}), 403
    item = query("SELECT ci.*, c.couple_id FROM carnet_items ci "
                 "JOIN carnets c ON c.id=ci.carnet_id WHERE ci.id=?",
                 (item_id,), one=True)
    if not item or item['couple_id'] != current_espace_id():
        return jsonify({'ok': False, 'error': '404'}), 404
    # v5.12 : suppression DEFAISABLE (corbeille) au lieu d'un DELETE
    execute("UPDATE carnet_items SET deleted_at=CURRENT_TIMESTAMP WHERE id=?", (item_id,))
    return jsonify({'ok': True})


@app.route('/item/<int:item_id>/restaurer', methods=['POST'])
@couple_required
def item_restaurer(item_id):
    """v5.12 — Defait la suppression d'un item (etape, lien, note...)."""
    if not csrf_check():
        return jsonify({'ok': False, 'error': 'CSRF'}), 403
    item = query("SELECT ci.*, c.couple_id FROM carnet_items ci "
                 "JOIN carnets c ON c.id=ci.carnet_id WHERE ci.id=?",
                 (item_id,), one=True)
    if not item or item['couple_id'] != current_espace_id():
        return jsonify({'ok': False, 'error': '404'}), 404
    execute("UPDATE carnet_items SET deleted_at=NULL WHERE id=?", (item_id,))
    return jsonify({'ok': True})


@app.route('/carnet/<int:cid_carnet>/transformer', methods=['GET', 'POST'])
@couple_required
def carnet_transformer(cid_carnet):
    """Transforme un carnet de souhait en carnet de voyage.
    POST atomique : cree le voyage, deplace les items selectionnes, lie parent."""
    c = _get_carnet_or_404(cid_carnet)
    if c['type'] != 'souhait':
        flash("Seul un carnet de souhait peut etre transforme.", "err")
        return redirect(url_for('carnet_view', cid_carnet=cid_carnet))

    items = _carnet_items(cid_carnet)
    if request.method == 'POST':
        if not csrf_check():
            flash("Session expiree.", "err")
            return redirect(url_for('carnet_transformer', cid_carnet=cid_carnet))
        title = (request.form.get('title') or c['title']).strip()
        location = (request.form.get('location') or c['location'] or '').strip()
        date_start = (request.form.get('date_start') or '').strip() or None
        date_end = (request.form.get('date_end') or '').strip() or None
        # v5.1 : l'ordre envoye est la concatenation des jours — une epingle
        # partagee entre deux jours y apparait deux fois. On DEDUPLIQUE en
        # gardant la premiere occurrence : un item ne se deplace qu'une fois.
        selected_ids = list(dict.fromkeys(
            int(x) for x in request.form.getlist('item_ids') if str(x).isdigit()))
        # CLOISONNEMENT (D1) : on ne transforme QUE des items de CE carnet.
        # Sans ce filtre, un POST forge avec l'id d'un item d'un autre espace
        # le copiait — et en mode deplacement, le VOLAIT. Trou pre-existant,
        # ferme ici parce qu'on touche deja a cette route (2026-07-27).
        if selected_ids:
            ph_own = ','.join('?' * len(selected_ids))
            a_moi = {r['id'] for r in query(
                f"SELECT id FROM carnet_items WHERE carnet_id=? AND id IN ({ph_own})",
                tuple([cid_carnet] + selected_ids))}
            selected_ids = [i for i in selected_ids if i in a_moi]
        duplicate = request.form.get('duplicate') == '1'

        # Atomique : transaction
        conn = get_db()
        try:
            cur = conn.execute(
                "INSERT INTO carnets (couple_id, title, type, location, date_start, "
                "date_end, status, created_by, parent_souhait_id) VALUES (?,?,?,?,?,?,?,?,?)",
                (c['couple_id'], title, 'voyage', location, date_start, date_end,
                 'active', session['uid'], cid_carnet)
            )
            new_cid = cur.lastrowid
            # v4.1 : ordered=1 (parcours trace sur la carte) -> les positions
            # dans le voyage suivent l'ordre de selection des etapes.
            ordered = request.form.get('ordered') == '1'
            id_map = {}          # v5.1 : item du souhait -> item du voyage (mode copie)
            if selected_ids:
                placeholders = ','.join('?' * len(selected_ids))
                if duplicate:
                    # v5.1 : boucle dans les deux cas (au lieu d'un INSERT..SELECT
                    # en bloc) pour recuperer l'id de chaque copie — sans quoi les
                    # trajet_steps ne peuvent pas suivre le voyage.
                    for pos, iid in enumerate(selected_ids):
                        if ordered:
                            sql_pos, params = "?", (new_cid, pos, iid)
                        else:
                            sql_pos, params = "position", (new_cid, iid)
                        cur2 = conn.execute(
                            f"INSERT INTO carnet_items (carnet_id, position, kind, title, "
                            f"body, url, photo_id, address, geo_lat, geo_lng, amount, currency, "
                            f"pin_kind, planned_day, parent_item_id, added_by) "
                            f"SELECT ?, {sql_pos}, kind, title, body, url, photo_id, address, "
                            f"geo_lat, geo_lng, amount, currency, pin_kind, planned_day, "
                            f"parent_item_id, added_by "
                            f"FROM carnet_items WHERE id=?",
                            params
                        )
                        id_map[iid] = cur2.lastrowid
                else:
                    # Deplacer : changer carnet_id (le souhait n'a plus l'item)
                    conn.execute(
                        f"UPDATE carnet_items SET carnet_id=?, target_carnet_id=? "
                        f"WHERE id IN ({placeholders})",
                        tuple([new_cid, new_cid] + selected_ids)
                    )
                    if ordered:
                        for pos, iid in enumerate(selected_ids):
                            conn.execute(
                                "UPDATE carnet_items SET position=? WHERE id=?",
                                (pos, iid)
                            )
                # v5.2 : les BLOCS de la journee suivent le voyage — modes,
                # heures et ordre compris. Une etape peut figurer dans deux
                # blocs ou deux jours : c'est voulu, on ne deduplique pas ici.
                if duplicate:
                    for tr in conn.execute(
                            "SELECT id, day, ordre, mode, heure, fait FROM trajets "
                            "WHERE carnet_id=? ORDER BY day, ordre, id",
                            (cid_carnet,)).fetchall():
                        steps = conn.execute(
                            "SELECT position, item_id FROM trajet_steps "
                            "WHERE trajet_id=? ORDER BY position, id", (tr['id'],)
                        ).fetchall()
                        copies = [(s['position'], id_map[s['item_id']])
                                  for s in steps if s['item_id'] in id_map]
                        if not copies:
                            continue          # aucune etape copiee : pas de bloc vide
                        ntid = conn.execute(
                            "INSERT INTO trajets (carnet_id, day, ordre, mode, heure, fait) "
                            "VALUES (?,?,?,?,?,?)",
                            (new_cid, tr['day'], tr['ordre'], tr['mode'],
                             tr['heure'], tr['fait'])
                        ).lastrowid
                        for p, (_, nid) in enumerate(copies):
                            conn.execute(
                                "INSERT INTO trajet_steps (trajet_id, position, item_id) "
                                "VALUES (?,?,?)", (ntid, p, nid))
                else:
                    # les blocs partent avec le carnet...
                    conn.execute("UPDATE trajets SET carnet_id=? WHERE carnet_id=?",
                                 (new_cid, cid_carnet))
                    # ...puis on retire les etapes dont l'epingle est restee au souhait
                    conn.execute(
                        "DELETE FROM trajet_steps WHERE trajet_id IN "
                        "(SELECT id FROM trajets WHERE carnet_id=?) AND item_id NOT IN "
                        "(SELECT id FROM carnet_items WHERE carnet_id=?)",
                        (new_cid, new_cid)
                    )
                # Filet : aucun bloc vide ne survit (ni ici ni la-bas)
                conn.execute(
                    "DELETE FROM trajets WHERE carnet_id IN (?,?) AND NOT EXISTS "
                    "(SELECT 1 FROM trajet_steps s WHERE s.trajet_id = trajets.id)",
                    (cid_carnet, new_cid)
                )
            conn.commit()
        except Exception as e:
            conn.rollback()
            log.error("transformation echec: %s\n%s", e, traceback.format_exc())
            flash("Erreur lors de la transformation : " + str(e), "err")
            return redirect(url_for('carnet_transformer', cid_carnet=cid_carnet))
        finally:
            conn.close()
        _log_activity(c['couple_id'], session['uid'], 'carnet_transformed',
                      target_carnet_id=new_cid,
                      payload={'title': title, 'from_souhait_id': cid_carnet})
        flash("Carnet de voyage cree depuis ton souhait.", "ok")
        return redirect(url_for('carnet_album', cid_carnet=new_cid))

    return render_template('carnet_transformer.html', carnet=c, items=items)


@app.route('/carnet/<int:cid_carnet>/modifier', methods=['GET', 'POST'])
@couple_required
def carnet_modifier(cid_carnet):
    c = _get_carnet_or_404(cid_carnet)
    if request.method == 'POST':
        if not csrf_check():
            flash("Session expiree.", "err")
            return redirect(url_for('carnet_modifier', cid_carnet=cid_carnet))
        data, errors = _parse_carnet_form(request.form)
        if errors:
            for e in errors:
                flash(e, "err")
            return render_template('carnet_form.html', mode='modifier', carnet=data, types=CARNET_TYPES, cid_carnet=cid_carnet)
        execute(
            "UPDATE carnets SET title=?, type=?, location=?, date_start=?, date_end=?, "
            "updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (data['title'], data['type'], data['location'],
             data['date_start'], data['date_end'], cid_carnet)
        )
        return redirect(url_for('carnet_view', cid_carnet=cid_carnet))
    return render_template('carnet_form.html', mode='modifier', carnet=c, types=CARNET_TYPES, cid_carnet=cid_carnet)


@app.route('/carnet/<int:cid_carnet>/supprimer', methods=['POST'])
@couple_required
def carnet_supprimer(cid_carnet):
    c = _get_carnet_or_404(cid_carnet)
    if not csrf_check():
        flash("Session expiree.", "err")
        return redirect(url_for('carnet_view', cid_carnet=cid_carnet))
    execute(
        "UPDATE carnets SET deleted_at=CURRENT_TIMESTAMP WHERE id=?",
        (cid_carnet,)
    )
    # v5.11 : la suppression se DEFAIT — le carnet part a la corbeille de
    # l'accueil (ou des reveries), d'ou il se restaure en un geste.
    flash("Carnet envoye a la corbeille — restaurable en bas de page.", "ok")
    _log_activity(c['couple_id'], session['uid'], 'carnet_deleted',
                  target_carnet_id=cid_carnet, payload={'title': c.get('title')})
    if c.get('type') == 'souhait':
        return redirect(url_for('souhaits_index'))
    return redirect(url_for('home'))


@app.route('/carnet/<int:cid_carnet>/restaurer', methods=['POST'])
@couple_required
def carnet_restaurer(cid_carnet):
    """v5.11 — Defait une suppression de carnet (deleted_at remis a NULL).
    Cloisonne : uniquement un carnet de l'espace courant."""
    c = query("SELECT * FROM carnets WHERE id=? AND couple_id=?",
              (cid_carnet, current_espace_id()), one=True)
    if not c:
        abort(404)
    if not csrf_check():
        flash("Session expiree.", "err")
        return redirect(url_for('home'))
    execute("UPDATE carnets SET deleted_at=NULL WHERE id=?", (cid_carnet,))
    flash(f"« {c['title']} » restauré.", "ok")
    _log_activity(c['couple_id'], session['uid'], 'carnet_restored',
                  target_carnet_id=cid_carnet, payload={'title': c['title']})
    if c['type'] == 'souhait':
        return redirect(url_for('souhaits_index'))
    return redirect(url_for('home'))


# ══════════════════════════════════════════════════════════════════════
#                    v1.5 — APERCU LIVRE + EXPORT PDF
# ══════════════════════════════════════════════════════════════════════

PDF_FORMATS = {
    'square_20':     ('Carre 20×20 cm',   200, 200),
    'landscape_a4':  ('A4 paysage',       297, 210),
    'portrait_a5':   ('A5 portrait',      148, 210),
}


# ══════════════════════════════════════════════════════════════════════
#                v2.4 — CARTOGRAPHIE PDF (Brief 06 §5)
# ══════════════════════════════════════════════════════════════════════
import urllib.request
import urllib.parse
import hashlib

MAPS_CACHE_DIR = os.path.join(os.path.dirname(DB_PATH), 'maps_cache')
os.makedirs(MAPS_CACHE_DIR, exist_ok=True)
MAP_CACHE_TTL_DAYS = 90


def _staticmap_url(center_lat, center_lng, zoom, width, height, markers=None):
    """URL staticmap.openstreetmap.de avec markers optionnels."""
    base = "https://staticmap.openstreetmap.de/staticmap.php"
    params = {
        'center': f"{center_lat:.5f},{center_lng:.5f}",
        'zoom': str(zoom),
        'size': f"{width}x{height}",
        'maptype': 'mapnik',
    }
    qs = urllib.parse.urlencode(params)
    if markers:
        marker_parts = []
        for lat, lng in markers[:30]:
            marker_parts.append(f"markers={lat:.5f},{lng:.5f},red-pushpin")
        if marker_parts:
            qs += '&' + '&'.join(marker_parts)
    return base + '?' + qs


_TILE_UA = 'Notre-Histoire/1.0 (+arthur.kembellec@gmail.com)'


def _latlng_to_tile_xy(lat, lng, zoom):
    import math
    lat_rad = math.radians(max(-85.0511, min(85.0511, lat)))
    n = 2.0 ** zoom
    x = (lng + 180.0) / 360.0 * n
    y = (1.0 - math.log(math.tan(lat_rad) + 1 / math.cos(lat_rad)) / math.pi) / 2.0 * n
    return x, y


def _fetch_osm_tile(z, x, y):
    """Fetch une tuile OSM 256x256 avec cache disque."""
    n = 2 ** z
    if y < 0 or y >= n:
        return None
    x = x % n
    tdir = os.path.join(MAPS_CACHE_DIR, 'tiles', str(z), str(x))
    os.makedirs(tdir, exist_ok=True)
    cache_path = os.path.join(tdir, f"{y}.png")
    if os.path.exists(cache_path):
        try:
            age_days = (datetime.utcnow().timestamp() - os.path.getmtime(cache_path)) / 86400
            if age_days < MAP_CACHE_TTL_DAYS:
                with open(cache_path, 'rb') as f:
                    return f.read()
        except Exception:
            pass
    url = f"https://tile.openstreetmap.org/{z}/{x}/{y}.png"
    try:
        req = urllib.request.Request(url, headers={'User-Agent': _TILE_UA})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = resp.read()
        with open(cache_path, 'wb') as f:
            f.write(data)
        return data
    except Exception as e:
        log.warning("OSM tile fetch fail z=%d x=%d y=%d: %s", z, x, y, e)
        return None


def _build_map_from_tiles(center_lat, center_lng, zoom, width, height, markers=None):
    """Compose une carte PNG width×height à partir de tuiles OSM 256x256.

    Trace les markers en pin terracotta. Retourne bytes ou None.
    """
    import math
    try:
        from PIL import Image as PILImage, ImageDraw
    except Exception:
        return None
    tile_size = 256
    cx, cy = _latlng_to_tile_xy(center_lat, center_lng, zoom)
    cx_px = cx * tile_size
    cy_px = cy * tile_size
    half_w = width / 2
    half_h = height / 2
    # Bbox en pixels monde
    px0 = cx_px - half_w
    py0 = cy_px - half_h
    px1 = cx_px + half_w
    py1 = cy_px + half_h
    tx0 = int(math.floor(px0 / tile_size))
    ty0 = int(math.floor(py0 / tile_size))
    tx1 = int(math.floor(px1 / tile_size))
    ty1 = int(math.floor(py1 / tile_size))
    canvas_w = (tx1 - tx0 + 1) * tile_size
    canvas_h = (ty1 - ty0 + 1) * tile_size
    canvas = PILImage.new('RGB', (canvas_w, canvas_h), '#e9e2d3')
    fetched = 0
    failed = 0
    for tx in range(tx0, tx1 + 1):
        for ty in range(ty0, ty1 + 1):
            tile_bytes = _fetch_osm_tile(zoom, tx, ty)
            if not tile_bytes:
                failed += 1
                continue
            try:
                tile_img = PILImage.open(io.BytesIO(tile_bytes)).convert('RGB')
                canvas.paste(tile_img,
                             ((tx - tx0) * tile_size, (ty - ty0) * tile_size))
                fetched += 1
            except Exception as e:
                failed += 1
                log.warning("tile decode fail z=%d x=%d y=%d: %s", zoom, tx, ty, e)
    if fetched == 0:
        return None
    # Crop sur la zone demandée
    crop_x = int(px0 - tx0 * tile_size)
    crop_y = int(py0 - ty0 * tile_size)
    img = canvas.crop((crop_x, crop_y, crop_x + width, crop_y + height))
    # Markers en pin terracotta
    if markers:
        draw = ImageDraw.Draw(img)
        for lat, lng in markers[:50]:
            mx, my = _latlng_to_tile_xy(lat, lng, zoom)
            mpx = int(mx * tile_size - px0)
            mpy = int(my * tile_size - py0)
            if 0 <= mpx < width and 0 <= mpy < height:
                # halo blanc cassé
                draw.ellipse([mpx - 7, mpy - 7, mpx + 7, mpy + 7],
                             fill='#FAF8F4', outline='#1C1A17', width=1)
                draw.ellipse([mpx - 4, mpy - 4, mpx + 4, mpy + 4],
                             fill='#C4654A')
    out = io.BytesIO()
    img.save(out, 'PNG')
    return out.getvalue()


def _fetch_staticmap_png(center_lat, center_lng, zoom, width, height, markers=None):
    """Fetch (avec cache local 90j) une staticmap PNG. Retourne bytes ou None.

    Stratégie : cache → tuiles OSM assemblées (fiable) → fallback staticmap.openstreetmap.de.
    """
    if center_lat is None or center_lng is None:
        return None
    # Clé cache (incluant markers)
    key = f"{center_lat:.5f}_{center_lng:.5f}_{zoom}_{width}_{height}"
    if markers:
        marker_str = '|'.join(f"{a:.5f},{b:.5f}" for a, b in markers[:30])
        key += '_' + hashlib.md5(marker_str.encode()).hexdigest()[:8]
    safe_key = hashlib.md5(key.encode()).hexdigest()
    cache_path = os.path.join(MAPS_CACHE_DIR, f"{safe_key}.png")
    if os.path.exists(cache_path):
        try:
            age_days = (datetime.utcnow().timestamp() - os.path.getmtime(cache_path)) / 86400
            if age_days < MAP_CACHE_TTL_DAYS:
                with open(cache_path, 'rb') as f:
                    return f.read()
        except Exception:
            pass
    # 1) Assemblage tuiles OSM (fiable, contrôle total markers)
    data = None
    try:
        data = _build_map_from_tiles(center_lat, center_lng, zoom, width, height, markers)
    except Exception as e:
        log.warning("OSM tile build fail: %s", e)
    # 2) Fallback staticmap.openstreetmap.de
    if not data:
        url = _staticmap_url(center_lat, center_lng, zoom, width, height, markers)
        try:
            req = urllib.request.Request(url, headers={'User-Agent': _TILE_UA})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = resp.read()
        except Exception as e:
            log.warning("staticmap fetch fail (%s): %s", url[:100], e)
            return None
    if data:
        try:
            with open(cache_path, 'wb') as f:
                f.write(data)
        except Exception as e:
            log.warning("map cache write fail: %s", e)
    return data


def _compute_map_zoom(min_lat, max_lat, min_lng, max_lng, width_px, height_px):
    """Calcule le zoom OSM pour englober le bbox (Mercator approx)."""
    import math
    if min_lat is None or max_lat is None:
        return 2
    lat_span = max(abs(max_lat - min_lat), 0.001)
    lng_span = max(abs(max_lng - min_lng), 0.001)
    zoom_lng = math.log2(360.0 * (width_px / 256.0) / lng_span)
    zoom_lat = math.log2(170.0 * (height_px / 256.0) / lat_span)
    z = int(min(zoom_lat, zoom_lng)) - 1
    return max(2, min(15, z))


def _carnet_geo_summary(carnet_id):
    """Retourne dict {markers, center, bbox, count} ou None si pas de GPS.

    v5.17 : les ETAPES du planning (carnet_items kind=location) rejoignent
    la carte d'ensemble du livre — l'utilisateur prepare ses lieux sur la
    carte de planification, le livre les reutilise (retour d'Arthur)."""
    rows = query("""
        SELECT p.gps_lat, p.gps_lng FROM photos p
        JOIN album_pages ap ON ap.photo_id = p.id
        WHERE ap.carnet_id = ? AND p.gps_lat IS NOT NULL AND p.gps_lng IS NOT NULL
          AND COALESCE(ap.is_hidden, 0) = 0
        UNION
        SELECT v.gps_lat, v.gps_lng FROM videos v
        JOIN album_pages ap ON ap.video_id = v.id
        WHERE ap.carnet_id = ? AND v.gps_lat IS NOT NULL AND v.gps_lng IS NOT NULL
          AND COALESCE(ap.is_hidden, 0) = 0
        UNION
        SELECT ci.geo_lat, ci.geo_lng FROM carnet_items ci
        WHERE ci.carnet_id = ? AND ci.kind = 'location'
          AND ci.geo_lat IS NOT NULL AND ci.geo_lng IS NOT NULL
          AND ci.deleted_at IS NULL
    """, (carnet_id, carnet_id, carnet_id))
    coords = []
    seen = set()
    for r in rows:
        lat, lng = r['gps_lat'], r['gps_lng']
        if lat is None or lng is None:
            continue
        key = (round(lat * 200) / 200, round(lng * 200) / 200)
        if key in seen:
            continue
        seen.add(key)
        coords.append((lat, lng))
    if not coords:
        return None
    lats = [c[0] for c in coords]
    lngs = [c[1] for c in coords]
    return {
        'markers': coords,
        'center_lat': (min(lats) + max(lats)) / 2,
        'center_lng': (min(lngs) + max(lngs)) / 2,
        'min_lat': min(lats), 'max_lat': max(lats),
        'min_lng': min(lngs), 'max_lng': max(lngs),
        'count': len(coords),
    }

PDF_LAYOUTS = [
    ('1', '1 photo / page'),
    ('2', '2 photos / page'),
    ('3', '3 photos / page'),
    ('4', '4 photos / page'),
]

PDF_MARGIN_POSITIONS = [
    ('outer',  'Notes côté extérieur'),
    ('inner',  'Notes côté reliure'),
    ('bottom', 'Notes en bas'),
    ('end',    'Notes en fin de livre'),
    # Alias rétro-compat (anciens carnets stockés avec right/left)
    ('right',  'Notes à droite'),
    ('left',   'Notes à gauche'),
]


# ══════════════════════════════════════════════════════════════════════
#       v3.2 — Fil d'activité partagé (couple)
# ══════════════════════════════════════════════════════════════════════
import json as _json


def _log_activity(couple_id, actor_user_id, kind, *,
                  target_carnet_id=None, payload=None):
    """Insère un événement dans le fil d'activité du couple.

    payload : dict sérialisé en JSON (ex : {'count': 3, 'title': 'Auvergne'}).
    Échec silencieux : on ne casse jamais la requête principale.
    """
    try:
        if not couple_id:
            return
        pl = _json.dumps(payload or {}, ensure_ascii=False)
        execute(
            "INSERT INTO activity_events (couple_id, actor_user_id, kind, "
            "target_carnet_id, payload) VALUES (?,?,?,?,?)",
            (couple_id, actor_user_id, kind, target_carnet_id, pl)
        )
    except Exception as _e:
        log.warning("activity log fail (%s): %s", kind, _e)


def _list_activity(couple_id, limit=50):
    """Renvoie les N derniers événements + auteur + carnet (jointure)."""
    rows = query("""
        SELECT ae.*, u.display_name AS actor_name,
               c.title AS carnet_title, c.type AS carnet_type
        FROM activity_events ae
        LEFT JOIN users u ON u.id = ae.actor_user_id
        LEFT JOIN carnets c ON c.id = ae.target_carnet_id
        WHERE ae.couple_id = ?
        ORDER BY ae.created_at DESC, ae.id DESC
        LIMIT ?
    """, (couple_id, limit))
    out = []
    for r in rows:
        d = dict(r)
        try:
            d['payload_data'] = _json.loads(d.get('payload') or '{}')
        except Exception:
            d['payload_data'] = {}
        out.append(d)
    return out


def _count_unseen_activity(user_id, couple_id):
    """Combien d'événements non vus par cet utilisateur dans cet espace.

    On ignore les événements dont l'utilisateur est lui-même l'auteur.
    """
    if not (user_id and couple_id):
        return 0
    try:
        seen = query(
            "SELECT last_seen_at FROM activity_seen WHERE user_id=? AND couple_id=?",
            (user_id, couple_id), one=True
        )
        last = seen['last_seen_at'] if seen else None
        if last:
            r = query(
                "SELECT COUNT(*) AS n FROM activity_events "
                "WHERE couple_id=? AND created_at > ? "
                "AND COALESCE(actor_user_id, 0) != ?",
                (couple_id, last, user_id), one=True
            )
        else:
            r = query(
                "SELECT COUNT(*) AS n FROM activity_events "
                "WHERE couple_id=? AND COALESCE(actor_user_id, 0) != ?",
                (couple_id, user_id), one=True
            )
        return r['n'] if r else 0
    except Exception:
        return 0


def _mark_activity_seen(user_id, couple_id):
    """Marque tous les événements de cet espace comme vus par l'utilisateur."""
    if not (user_id and couple_id):
        return
    try:
        execute(
            "INSERT INTO activity_seen (user_id, couple_id, last_seen_at) "
            "VALUES (?, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(user_id, couple_id) DO UPDATE SET "
            "last_seen_at = CURRENT_TIMESTAMP",
            (user_id, couple_id)
        )
    except Exception as _e:
        log.warning("activity seen fail: %s", _e)


@app.route('/activite')
@couple_required
def activity_feed():
    """Fil d'activité partagé du couple."""
    eid = current_espace_id()
    events = _list_activity(eid, limit=80)
    _mark_activity_seen(session['uid'], eid)
    return render_template('activite.html', events=events)


# ══════════════════════════════════════════════════════════════════════
#       v3.0 — Adaptateur souhait : carnet_items → pages_data
# ══════════════════════════════════════════════════════════════════════
def _souhait_pages_for_book(cid_carnet):
    """Adapte les carnet_items d'un souhait en pages_data pour pdf_book.

    main : items kind='photo' (avec photo_path) → pseudo album_pages
    margin : items kind='link'/'note'/'budget'/'location' → notes contextuelles
    """
    items = _carnet_items(cid_carnet)
    main = []
    margin = []
    for it in items:
        if it.get('kind') == 'photo' and it.get('photo_path'):
            main.append({
                'id': it['id'],
                'photo_id': it.get('photo_id'),
                'photo_path': it['photo_path'],
                'photo_thumb': it.get('photo_thumb'),
                'photo_gps_lat': it.get('photo_gps_lat'),
                'photo_gps_lng': it.get('photo_gps_lng'),
                'caption': (it.get('title') or '') + (
                    f" — {it['body']}" if (it.get('title') and it.get('body')) else (it.get('body') or '')
                ),
                'full_bleed_override': None,
                'type': 'photo',
                'section_id': None,
            })
        else:
            text_parts = []
            if it.get('title'):   text_parts.append(it['title'])
            if it.get('body'):    text_parts.append(it['body'])
            if it.get('url'):     text_parts.append(it['url'])
            if it.get('address'): text_parts.append(it['address'])
            if it.get('amount') is not None:
                text_parts.append(f"{it['amount']:.0f} {it.get('currency') or 'EUR'}")
            margin.append({
                'id': it['id'],
                'caption': '\n'.join(p for p in text_parts if p),
                'text_content': it.get('body') or '',
                'photo_thumb': it.get('photo_thumb'),
                'photo_path': it.get('photo_path'),
                'type': 'text' if not it.get('photo_path') else 'photo',
            })
    return {'main': main, 'margin': margin}


def _souhait_geo_summary(cid_carnet):
    """Geo summary pour un souhait : items kind='location' + photos GPS."""
    items = _carnet_items(cid_carnet)
    coords = []
    for it in items:
        if (it.get('kind') == 'location'
                and it.get('geo_lat') is not None
                and it.get('geo_lng') is not None):
            coords.append((it['geo_lat'], it['geo_lng']))
        elif (it.get('photo_gps_lat') is not None
              and it.get('photo_gps_lng') is not None):
            coords.append((it['photo_gps_lat'], it['photo_gps_lng']))
    if not coords:
        return None
    lats = [c[0] for c in coords]
    lngs = [c[1] for c in coords]
    return {
        'markers': coords,
        'center_lat': (min(lats) + max(lats)) / 2,
        'center_lng': (min(lngs) + max(lngs)) / 2,
        'min_lat': min(lats), 'max_lat': max(lats),
        'min_lng': min(lngs), 'max_lng': max(lngs),
        'count': len(coords),
    }


# ══════════════════════════════════════════════════════════════════════
#                v3.0 — MOTEUR PDF PRO (pdf_book.py)
# ══════════════════════════════════════════════════════════════════════
try:
    import pdf_book as _pdf_book
    _PDF_BOOK_OK = True
except Exception as _e:
    _pdf_book = None
    _PDF_BOOK_OK = False
    log.warning("pdf_book non disponible : %s", _e)


def _section_coords_for_chunk_v3(items_chunk):
    """Mini-carte en marge : retrouve coords + label de la section d'un chunk.

    Retourne {lat, lng, label, zoom} ou None.
    """
    if not items_chunk:
        return None
    section_ids = set(it.get('section_id') for it in items_chunk if it.get('section_id'))
    if not section_ids:
        for it in items_chunk:
            if it.get('photo_gps_lat') is not None and it.get('photo_gps_lng') is not None:
                return {
                    'lat': it['photo_gps_lat'],
                    'lng': it['photo_gps_lng'],
                    'label': '',
                    'zoom': 13,
                }
        return None
    if len(section_ids) == 1:
        sid = next(iter(section_ids))
        sec = query(
            "SELECT location_lat, location_lng, location_name, primary_label "
            "FROM album_sections WHERE id=?",
            (sid,), one=True
        )
        if sec and sec['location_lat'] is not None:
            return {
                'lat': sec['location_lat'],
                'lng': sec['location_lng'],
                'label': sec['location_name'] or sec['primary_label'] or '',
                'zoom': 12,
            }
    return None


@app.route('/carnet/<int:cid_carnet>/cover_set', methods=['POST'])
@couple_required
def carnet_cover_set(cid_carnet):
    """Définit la photo de couverture du carnet à partir d'une page album."""
    _get_carnet_or_404(cid_carnet)
    if not csrf_check():
        return jsonify({'ok': False, 'error': 'CSRF'}), 403
    page_id = request.form.get('page_id', type=int)
    if not page_id:
        return jsonify({'ok': False, 'error': 'page_id requis'}), 400
    page = query(
        "SELECT photo_id FROM album_pages WHERE id=? AND carnet_id=?",
        (page_id, cid_carnet), one=True
    )
    if not page or not page['photo_id']:
        return jsonify({'ok': False, 'error': 'Page sans photo'}), 404
    execute(
        "UPDATE carnets SET cover_photo_id=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (page['photo_id'], cid_carnet)
    )
    return jsonify({'ok': True, 'cover_photo_id': page['photo_id']})


@app.route('/album_page/<int:page_id>/full_mode', methods=['POST'])
@couple_required
def album_page_full_mode(page_id):
    """Bascule le mode plein-page d'une photo : normal / full / spread."""
    if not csrf_check():
        return jsonify({'ok': False, 'error': 'CSRF'}), 403
    mode = request.form.get('mode', 'normal')
    mapping = {'normal': 0, 'full': 1, 'spread': 2}
    if mode not in mapping:
        return jsonify({'ok': False, 'error': 'mode invalide'}), 400
    page = query(
        "SELECT ap.id FROM album_pages ap "
        "JOIN carnets c ON c.id = ap.carnet_id "
        "WHERE ap.id=? AND c.couple_id=?",
        (page_id, current_espace_id()), one=True
    )
    if not page:
        return jsonify({'ok': False, 'error': 'Non autorisé'}), 404
    val = mapping[mode] if mapping[mode] != 0 else None
    execute(
        "UPDATE album_pages SET full_bleed_override=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (val, page_id)
    )
    return jsonify({'ok': True, 'mode': mode, 'value': val})


@app.route('/carnet/<int:cid_carnet>/apercu')
@couple_required
def carnet_apercu(cid_carnet):
    """Page apercu HTML paginée du livre photo (v3 : double page + cover picker)."""
    c = _get_carnet_or_404(cid_carnet)
    sort_mode = c.get('sort_mode') or 'chrono'
    # v3 : extension souhait — convertir carnet_items en pseudo album_pages
    if c['type'] == 'souhait':
        pages_data = _souhait_pages_for_book(cid_carnet)
    else:
        pages_data = _carnet_pages(cid_carnet, sort_mode=sort_mode)
    fmt = request.args.get('format', 'square_20')
    if fmt not in PDF_FORMATS:
        fmt = 'square_20'
    layout = request.args.get('layout', c.get('pdf_layout') or '1')
    if layout not in dict(PDF_LAYOUTS):
        layout = '1'
    margin_pos = request.args.get('margin', c.get('pdf_margin_position') or 'outer')
    if margin_pos not in dict(PDF_MARGIN_POSITIONS):
        margin_pos = 'outer'
    # v3 : couverture résolue (cover_photo_id ou fallback 1ère photo)
    cover_item = None
    if c.get('cover_photo_id'):
        for p in pages_data['main']:
            if p.get('photo_id') == c['cover_photo_id']:
                cover_item = p
                break
    if cover_item is None:
        cover_item = next((p for p in pages_data['main'] if p.get('photo_path')), None)
    # v4.4 : QR codes des videos (scanne le livre -> la video se lance)
    video_qrs = {}
    for p in pages_data['main']:
        if p.get('video_token'):
            try:
                video_qrs[p['id']] = qr_svg(
                    url_for('video_public', token=p['video_token'], _external=True))
            except Exception as e:
                log.warning("qr video %s: %s", p.get('id'), e)
    # Géo : présence de coordonnées sur le carnet
    geo_count = 0
    try:
        if c['type'] == 'souhait':
            geo_count = sum(1 for p in pages_data['main']
                            if p.get('photo_gps_lat') is not None)
        else:
            gs = _carnet_geo_summary(cid_carnet)
            if gs:
                geo_count = gs['count']
    except Exception:
        pass
    # v3.4.1 : plan d'alignement des notes de marge par date (partage avec le PDF)
    margin_plan = []
    if margin_pos != 'end' and pages_data['margin'] and _PDF_BOOK_OK and _pdf_book is not None:
        try:
            margin_plan = _pdf_book.build_margin_plan(
                pages_data['main'], pages_data['margin'], int(layout))
        except Exception as _e:
            log.warning("build_margin_plan fail: %s", _e)
    return render_template('apercu.html',
        carnet=c,
        main_pages=pages_data['main'],
        margin_pages=pages_data['margin'],
        margin_plan=margin_plan,
        format=fmt, layout=layout, margin_pos=margin_pos,
        video_qrs=video_qrs,
        formats=PDF_FORMATS, layouts=PDF_LAYOUTS, margin_positions=PDF_MARGIN_POSITIONS,
        cover_item=cover_item,
        geo_count=geo_count,
    )


@app.route('/carnet/<int:cid_carnet>/pdf/settings', methods=['POST'])
@couple_required
def carnet_pdf_settings(cid_carnet):
    """Sauve les reglages PDF (layout + margin_position) sur le carnet."""
    c = _get_carnet_or_404(cid_carnet)
    if not csrf_check():
        return jsonify({'ok': False, 'error': 'CSRF'}), 403
    layout = request.form.get('layout', '1')
    if layout not in dict(PDF_LAYOUTS):
        layout = '1'
    margin_pos = request.form.get('margin', 'right')
    if margin_pos not in dict(PDF_MARGIN_POSITIONS):
        margin_pos = 'right'
    execute("UPDATE carnets SET pdf_layout=?, pdf_margin_position=?, "
            "updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (layout, margin_pos, cid_carnet))
    return jsonify({'ok': True, 'layout': layout, 'margin_position': margin_pos})


@app.route('/carnet/<int:cid_carnet>/pdf')
@couple_required
def carnet_pdf(cid_carnet):
    """Genere le PDF du livre photo a la volee.

    Délègue au moteur pro pdf_book.render_carnet_pdf si disponible (v3),
    sinon retombe sur le rendu legacy embarqué (v2).
    """
    c = _get_carnet_or_404(cid_carnet)
    fmt = request.args.get('format', 'square_20')
    if fmt not in PDF_FORMATS:
        fmt = 'square_20'
    layout = request.args.get('layout', c.get('pdf_layout') or '1')
    if layout not in dict(PDF_LAYOUTS):
        layout = '1'
    n_per_page = int(layout)
    margin_pos = request.args.get('margin', c.get('pdf_margin_position') or 'outer')
    if margin_pos not in dict(PDF_MARGIN_POSITIONS):
        margin_pos = 'outer'
    sort_mode = c.get('sort_mode') or 'chrono'
    if c['type'] == 'souhait':
        pages_data = _souhait_pages_for_book(cid_carnet)
    else:
        pages_data = _carnet_pages(cid_carnet, sort_mode=sort_mode)

    # ── v3 : déléguer à pdf_book si disponible ─────────────────────────
    if _PDF_BOOK_OK and _pdf_book is not None:
        try:
            geo_summary = (_souhait_geo_summary(cid_carnet)
                           if c['type'] == 'souhait'
                           else _carnet_geo_summary(cid_carnet))
            buf = _pdf_book.render_carnet_pdf(
                carnet=c,
                pages_data=pages_data,
                fmt_info=PDF_FORMATS[fmt],
                layout=layout,
                margin_pos=margin_pos,
                upload_dir=UPLOAD_DIR,
                show_overview_map=bool(c.get('pdf_show_overview_map', 1)),
                show_section_maps=bool(c.get('pdf_show_section_maps', 1)),
                show_letters=bool(c.get('pdf_show_letters', 1)),
                cover_photo_id=c.get('cover_photo_id'),
                geo_summary=geo_summary,
                fetch_static_map=_fetch_staticmap_png,
                compute_zoom=_compute_map_zoom,
                section_zone_map_resolver=_section_coords_for_chunk_v3,
                qr_make=qrcode.make,
                video_url_for=lambda token: url_for('video_public', token=token, _external=True),
            )
            safe_title = ''.join(ch if ch.isalnum() or ch in ('-', '_') else '_'
                                 for ch in c['title'])[:40] or 'carnet'
            fname = f"{safe_title}_{fmt}.pdf"
            from flask import Response
            return Response(buf.getvalue(),
                mimetype='application/pdf',
                headers={'Content-Disposition': f'attachment; filename="{fname}"'})
        except Exception as _e:
            log.error("pdf_book render fail, fallback legacy : %s", _e, exc_info=True)
            # tombe en legacy ci-dessous

    # ── Fallback legacy (v2) ───────────────────────────────────────────
    # Compat : ancien code attend right/left, pas outer/inner
    if margin_pos == 'outer':
        margin_pos = 'right'
    elif margin_pos == 'inner':
        margin_pos = 'left'

    from reportlab.pdfgen import canvas as pdf_canvas
    from reportlab.lib.units import mm
    from reportlab.lib.utils import ImageReader

    label, w_mm, h_mm = PDF_FORMATS[fmt]
    page_w, page_h = w_mm * mm, h_mm * mm
    margin = 10 * mm

    buf = io.BytesIO()
    pdf = pdf_canvas.Canvas(buf, pagesize=(page_w, page_h))
    pdf.setTitle(c['title'])
    pdf.setAuthor("Notre Histoire")

    # ─ Page 1 : Couverture ─────────────────────────────────────────
    pdf.setFillColorRGB(0.98, 0.972, 0.957)
    pdf.rect(0, 0, page_w, page_h, fill=1, stroke=0)
    pdf.setFillColorRGB(0.110, 0.102, 0.090)

    # Trouver une photo de couverture (premiere photo principale)
    cover = next((p for p in pages_data['main']
                  if p.get('photo_path')), None)
    if cover:
        try:
            cover_path = os.path.join(UPLOAD_DIR, cover['photo_path'])
            img = ImageReader(cover_path)
            iw, ih = img.getSize()
            ratio = min((page_w - 2*margin) / iw, (page_h * 0.55) / ih)
            dw, dh = iw * ratio, ih * ratio
            pdf.drawImage(img, (page_w - dw) / 2, page_h * 0.40,
                          width=dw, height=dh, mask='auto')
        except Exception as e:
            log.warning("PDF cover image fail: %s", e)

    # Titre
    pdf.setFont('Times-Italic', 36)
    pdf.drawCentredString(page_w / 2, page_h * 0.30, c['title'])

    # Sous-titre : lieu + dates
    sub = []
    if c.get('location'): sub.append(c['location'])
    if c.get('date_start') and c.get('date_end') and c['date_start'] != c['date_end']:
        sub.append(f"{c['date_start']} → {c['date_end']}")
    elif c.get('date_start'):
        sub.append(c['date_start'])
    if sub:
        pdf.setFont('Helvetica', 11)
        pdf.setFillColorRGB(0.42, 0.41, 0.38)
        pdf.drawCentredString(page_w / 2, page_h * 0.24, ' · '.join(sub))

    pdf.setFont('Helvetica', 8)
    pdf.setFillColorRGB(0.64, 0.611, 0.572)
    pdf.drawCentredString(page_w / 2, margin, "NOTRE HISTOIRE")

    pdf.showPage()

    # ─ Page 2 : Carte d'ensemble (Brief 06 §5.2) ───────────────────
    show_overview = c.get('pdf_show_overview_map', 1)
    show_minimaps = c.get('pdf_show_section_maps', 1)
    if show_overview:
        geo_summary = _carnet_geo_summary(cid_carnet)
        if geo_summary:
            pdf.setFillColorRGB(0.98, 0.972, 0.957)
            pdf.rect(0, 0, page_w, page_h, fill=1, stroke=0)

            pdf.setFont('Times-Italic', 28)
            pdf.setFillColorRGB(0.110, 0.102, 0.090)
            pdf.drawCentredString(page_w / 2, page_h - margin - 12*mm, "Notre voyage")

            pdf.setFont('Helvetica', 9)
            pdf.setFillColorRGB(0.42, 0.41, 0.38)
            pdf.drawCentredString(page_w / 2, page_h - margin - 22*mm,
                                  f"{geo_summary['count']} lieu(x) sur la carte")

            map_w_mm = (w_mm - 30)
            map_h_mm = (h_mm - 70)
            map_w_px = min(int(map_w_mm * 4), 1024)
            map_h_px = min(int(map_h_mm * 4), 1024)
            zoom = _compute_map_zoom(
                geo_summary['min_lat'], geo_summary['max_lat'],
                geo_summary['min_lng'], geo_summary['max_lng'],
                map_w_px, map_h_px
            )
            map_png = _fetch_staticmap_png(
                geo_summary['center_lat'], geo_summary['center_lng'],
                zoom, map_w_px, map_h_px,
                markers=geo_summary['markers']
            )
            if map_png:
                try:
                    map_x = margin + 15*mm
                    map_y = (page_h - map_h_mm * mm) / 2 - 5*mm
                    pdf.drawImage(ImageReader(io.BytesIO(map_png)), map_x, map_y,
                                  width=map_w_mm * mm, height=map_h_mm * mm,
                                  mask='auto')
                    pdf.setStrokeColorRGB(0.88, 0.85, 0.80)
                    pdf.setLineWidth(0.5)
                    pdf.rect(map_x, map_y, map_w_mm * mm, map_h_mm * mm, fill=0, stroke=1)
                except Exception as e:
                    log.warning("PDF overview map draw fail: %s", e)
                    pdf.setFont('Helvetica', 10)
                    pdf.setFillColorRGB(0.6, 0.6, 0.6)
                    pdf.drawCentredString(page_w / 2, page_h / 2, "Carte indisponible")
            else:
                pdf.setFont('Helvetica', 10)
                pdf.setFillColorRGB(0.6, 0.6, 0.6)
                pdf.drawCentredString(page_w / 2, page_h / 2, "Carte indisponible")

            pdf.setFont('Helvetica', 8)
            pdf.setFillColorRGB(0.64, 0.611, 0.572)
            pdf.drawCentredString(page_w / 2, margin, "© OpenStreetMap contributors")
            pdf.showPage()

    # ─ Pages : album principal ─────────────────────────────────────
    def _draw_photo_page(item):
        # Fond creme leger
        pdf.setFillColorRGB(0.98, 0.972, 0.957)
        pdf.rect(0, 0, page_w, page_h, fill=1, stroke=0)
        try:
            ph_path = os.path.join(UPLOAD_DIR, item['photo_path'])
            img = ImageReader(ph_path)
            iw, ih = img.getSize()
            avail_w = page_w - 2*margin
            avail_h = page_h * 0.78
            ratio = min(avail_w / iw, avail_h / ih)
            dw, dh = iw * ratio, ih * ratio
            x = (page_w - dw) / 2
            y = page_h - margin - dh
            pdf.drawImage(img, x, y, width=dw, height=dh, mask='auto')
        except Exception as e:
            log.warning("PDF photo fail: %s", e)
        # Caption
        if item.get('caption'):
            pdf.setFont('Times-Italic', 12)
            pdf.setFillColorRGB(0.24, 0.227, 0.207)
            _wrap_text(pdf, item['caption'], page_w / 2, page_h * 0.12,
                       max_width=page_w - 2*margin, line_height=15)
        # Date prise (discrete en bas)
        if item.get('photo_taken_at'):
            pdf.setFont('Helvetica', 7)
            pdf.setFillColorRGB(0.64, 0.611, 0.572)
            pdf.drawString(margin, margin / 2, str(item['photo_taken_at']).replace('T', ' ')[:16])
        pdf.showPage()

    def _draw_text_page(item):
        pdf.setFillColorRGB(0.98, 0.972, 0.957)
        pdf.rect(0, 0, page_w, page_h, fill=1, stroke=0)
        text = item.get('text_content') or ''
        if text:
            pdf.setFont('Times-Italic', 18)
            pdf.setFillColorRGB(0.110, 0.102, 0.090)
            _wrap_text(pdf, text, page_w / 2, page_h / 2,
                       max_width=page_w - 4*margin, line_height=24)
        pdf.showPage()

    def _draw_video_page(item):
        # Page video : poster + QR vers /v/<token>
        pdf.setFillColorRGB(0.98, 0.972, 0.957)
        pdf.rect(0, 0, page_w, page_h, fill=1, stroke=0)
        try:
            poster_path = os.path.join(UPLOAD_DIR, item['video_poster'])
            img = ImageReader(poster_path)
            iw, ih = img.getSize()
            avail_w = page_w - 2*margin
            avail_h = page_h * 0.62
            ratio = min(avail_w / iw, avail_h / ih)
            dw, dh = iw * ratio, ih * ratio
            x = (page_w - dw) / 2
            y = page_h - margin - dh
            pdf.drawImage(img, x, y, width=dw, height=dh, mask='auto')
            # Petit play overlay au centre du poster
            cx, cy = x + dw / 2, y + dh / 2
            r = min(dw, dh) * 0.08
            pdf.setFillColorRGB(0, 0, 0, alpha=0.5)
            pdf.circle(cx, cy, r, stroke=0, fill=1)
            pdf.setFillColorRGB(1, 1, 1)
            tri = [(cx - r*0.4, cy - r*0.6),
                   (cx - r*0.4, cy + r*0.6),
                   (cx + r*0.6, cy)]
            p = pdf.beginPath()
            p.moveTo(*tri[0]); p.lineTo(*tri[1]); p.lineTo(*tri[2]); p.close()
            pdf.drawPath(p, stroke=0, fill=1)
        except Exception as e:
            log.warning("PDF video poster fail: %s", e)
        # QR code (vers la page publique)
        if item.get('video_token'):
            try:
                video_url = url_for('video_public', token=item['video_token'], _external=True)
                qr_img = qrcode.make(video_url)
                qr_buf = io.BytesIO()
                qr_img.save(qr_buf, 'PNG')
                qr_buf.seek(0)
                qr_size = 35 * mm
                qr_x = (page_w - qr_size) / 2
                qr_y = page_h * 0.13
                pdf.drawImage(ImageReader(qr_buf), qr_x, qr_y,
                              width=qr_size, height=qr_size, mask='auto')
                pdf.setFont('Helvetica', 9)
                pdf.setFillColorRGB(0.42, 0.41, 0.38)
                pdf.drawCentredString(page_w / 2, qr_y - 4*mm,
                                      "Scanne pour voir la video")
            except Exception as e:
                log.warning("PDF QR fail: %s", e)
        if item.get('caption'):
            pdf.setFont('Times-Italic', 10)
            pdf.setFillColorRGB(0.24, 0.227, 0.207)
            _wrap_text(pdf, item['caption'], page_w / 2, page_h * 0.06,
                       max_width=page_w - 2*margin, line_height=12)
        pdf.showPage()

    # ─ Helpers nouveau layout ──────────────────────────────────────
    def _draw_image_in_box(item, x, y, w, h):
        """Dessine une photo (avec caption optionnelle) dans une boite."""
        if not item.get('photo_path'):
            return
        try:
            img = ImageReader(os.path.join(UPLOAD_DIR, item['photo_path']))
            iw, ih = img.getSize()
            cap_h = 6 * mm if item.get('caption') else 0
            avail_h = h - cap_h
            ratio = min(w / iw, avail_h / ih)
            dw, dh = iw * ratio, ih * ratio
            cx = x + (w - dw) / 2
            cy = y + cap_h + (avail_h - dh)
            pdf.drawImage(img, cx, cy, width=dw, height=dh, mask='auto')
            if item.get('caption'):
                pdf.setFont('Times-Italic', 8.5)
                pdf.setFillColorRGB(0.24, 0.227, 0.207)
                _wrap_text(pdf, item['caption'], x + w/2, y + 2*mm,
                           max_width=w, line_height=10, max_lines=2)
        except Exception as e:
            log.warning("PDF image fail: %s", e)

    def _draw_video_in_box(item, x, y, w, h):
        """Dessine un poster video + petit play overlay + QR plus petit."""
        if not item.get('video_poster'):
            return
        try:
            img = ImageReader(os.path.join(UPLOAD_DIR, item['video_poster']))
            iw, ih = img.getSize()
            qr_size = min(w, h) * 0.22
            cap_h = 6 * mm if item.get('caption') else 0
            avail_h = h - cap_h - qr_size - 2*mm
            ratio = min(w / iw, avail_h / ih)
            dw, dh = iw * ratio, ih * ratio
            cx = x + (w - dw) / 2
            cy = y + cap_h + qr_size + 2*mm + (avail_h - dh)
            pdf.drawImage(img, cx, cy, width=dw, height=dh, mask='auto')
            # Play overlay
            ccx, ccy = cx + dw/2, cy + dh/2
            r = min(dw, dh) * 0.07
            pdf.setFillColorRGB(0, 0, 0, alpha=0.5)
            pdf.circle(ccx, ccy, r, stroke=0, fill=1)
            pdf.setFillColorRGB(1, 1, 1)
            p = pdf.beginPath()
            p.moveTo(ccx - r*0.4, ccy - r*0.6)
            p.lineTo(ccx - r*0.4, ccy + r*0.6)
            p.lineTo(ccx + r*0.6, ccy)
            p.close()
            pdf.drawPath(p, stroke=0, fill=1)
            # QR
            if item.get('video_token'):
                video_url = url_for('video_public', token=item['video_token'], _external=True)
                qr_img = qrcode.make(video_url)
                qr_buf = io.BytesIO(); qr_img.save(qr_buf, 'PNG'); qr_buf.seek(0)
                qr_x = x + (w - qr_size) / 2
                qr_y = y + cap_h
                pdf.drawImage(ImageReader(qr_buf), qr_x, qr_y,
                              width=qr_size, height=qr_size, mask='auto')
            if item.get('caption'):
                pdf.setFont('Times-Italic', 8.5)
                pdf.setFillColorRGB(0.24, 0.227, 0.207)
                _wrap_text(pdf, item['caption'], x + w/2, y + 2*mm,
                           max_width=w, line_height=10, max_lines=1)
        except Exception as e:
            log.warning("PDF video box fail: %s", e)

    def _draw_text_in_box(item, x, y, w, h):
        """Dessine un bloc texte centre dans une boite."""
        text = item.get('text_content') or ''
        if not text:
            return
        font_size = 11 if (w < 100*mm) else 16
        pdf.setFont('Times-Italic', font_size)
        pdf.setFillColorRGB(0.110, 0.102, 0.090)
        _wrap_text(pdf, text, x + w/2, y + h/2,
                   max_width=w - 4*mm, line_height=font_size*1.3, max_lines=10)

    def _draw_in_box(item, x, y, w, h):
        if item.get('video_path'):
            _draw_video_in_box(item, x, y, w, h)
        elif item.get('photo_path'):
            _draw_image_in_box(item, x, y, w, h)
        elif item.get('type') == 'text':
            _draw_text_in_box(item, x, y, w, h)

    def _grid_layout(n, area_x, area_y, area_w, area_h, gap=3*mm):
        """Retourne une liste de boites (x,y,w,h) pour disposer n photos."""
        boxes = []
        if n == 1:
            boxes.append((area_x, area_y, area_w, area_h))
        elif n == 2:
            # 2 photos : empile verticalement si zone plus haute que large
            if area_h > area_w:
                h = (area_h - gap) / 2
                boxes.append((area_x, area_y + h + gap, area_w, h))
                boxes.append((area_x, area_y, area_w, h))
            else:
                w = (area_w - gap) / 2
                boxes.append((area_x, area_y, w, area_h))
                boxes.append((area_x + w + gap, area_y, w, area_h))
        elif n == 3:
            # 1 grosse en haut + 2 dessous
            top_h = area_h * 0.55
            bot_h = area_h - top_h - gap
            half_w = (area_w - gap) / 2
            boxes.append((area_x, area_y + bot_h + gap, area_w, top_h))
            boxes.append((area_x, area_y, half_w, bot_h))
            boxes.append((area_x + half_w + gap, area_y, half_w, bot_h))
        else:  # n == 4
            half_w = (area_w - gap) / 2
            half_h = (area_h - gap) / 2
            boxes.append((area_x, area_y + half_h + gap, half_w, half_h))
            boxes.append((area_x + half_w + gap, area_y + half_h + gap, half_w, half_h))
            boxes.append((area_x, area_y, half_w, half_h))
            boxes.append((area_x + half_w + gap, area_y, half_w, half_h))
        return boxes

    def _draw_margin_zone(items, x, y, w, h, label_text):
        """Dessine une zone marge (mini photos + captions) dans un cadre."""
        if not items:
            return
        # Etiquette discrete
        pdf.setFont('Helvetica', 7)
        pdf.setFillColorRGB(0.64, 0.611, 0.572)
        if w > h:  # bandeau horizontal
            pdf.drawString(x, y + h - 3*mm, label_text)
        else:
            pdf.saveState()
            pdf.translate(x + 2*mm, y)
            pdf.rotate(90)
            pdf.drawString(0, -2*mm, label_text)
            pdf.restoreState()

        # Disposition des items
        n = len(items)
        if w > h:
            # Horizontal : aligner sur la largeur
            cell_w = (w - (n - 1) * 3*mm) / n if n > 0 else w
            for i, m in enumerate(items):
                cx = x + i * (cell_w + 3*mm)
                _draw_in_box(m, cx, y + 4*mm, cell_w, h - 6*mm)
        else:
            # Vertical : empiler
            cell_h = (h - (n - 1) * 3*mm) / n if n > 0 else h
            for i, m in enumerate(items):
                cy = y + (n - 1 - i) * (cell_h + 3*mm)
                _draw_in_box(m, x + 5*mm, cy, w - 7*mm, cell_h)

    # ─ Calcul : combien de notes en marge par page ────────────────
    main_filtered = [p for p in pages_data['main']]
    margin_items = pages_data['margin'][:] if margin_pos != 'end' else []

    # Distribuer les marges sur les pages photos
    nb_main_pages = max(1, (len(main_filtered) + n_per_page - 1) // n_per_page)
    margin_per_page = max(1, (len(margin_items) + nb_main_pages - 1) // nb_main_pages) if margin_items else 0

    margin_idx = 0

    def _take_margins(k):
        nonlocal margin_idx
        out = margin_items[margin_idx:margin_idx + k]
        margin_idx += len(out)
        return out

    # ─ Helper : coords du lieu commun d'un chunk (pour mini-map) ───
    def _section_coords_for_chunk(items_chunk):
        if not items_chunk:
            return None
        section_ids = set(it.get('section_id') for it in items_chunk if it.get('section_id'))
        if not section_ids:
            for it in items_chunk:
                if it.get('photo_gps_lat') is not None and it.get('photo_gps_lng') is not None:
                    return {
                        'lat': it['photo_gps_lat'], 'lng': it['photo_gps_lng'],
                        'label': '', 'zoom': 13,
                    }
            return None
        if len(section_ids) == 1:
            sid = next(iter(section_ids))
            sec = query(
                "SELECT location_lat, location_lng, location_name, primary_label "
                "FROM album_sections WHERE id=?",
                (sid,), one=True
            )
            if sec and sec['location_lat'] is not None:
                return {
                    'lat': sec['location_lat'], 'lng': sec['location_lng'],
                    'label': sec['location_name'] or sec['primary_label'] or '',
                    'zoom': 12,
                }
        return None

    # ─ Layout d'une page composite ────────────────────────────────
    def _draw_composite_page(items_chunk):
        pdf.setFillColorRGB(0.98, 0.972, 0.957)
        pdf.rect(0, 0, page_w, page_h, fill=1, stroke=0)

        if margin_pos == 'right':
            # Album : 70% gauche, marge : 28% droite
            album_w = (page_w - 2*margin) * 0.68
            margin_w = (page_w - 2*margin) - album_w - 4*mm
            album_x = margin
            album_y = margin
            album_h = page_h - 2*margin
            mzone_x = margin + album_w + 4*mm
            mzone_y = margin
            mzone_w = margin_w
            mzone_h = page_h - 2*margin
        elif margin_pos == 'left':
            margin_w = (page_w - 2*margin) * 0.30 - 2*mm
            album_w = (page_w - 2*margin) - margin_w - 4*mm
            mzone_x = margin
            mzone_y = margin
            mzone_w = margin_w
            mzone_h = page_h - 2*margin
            album_x = margin + margin_w + 4*mm
            album_y = margin
            album_h = page_h - 2*margin
        elif margin_pos == 'bottom':
            margin_h = (page_h - 2*margin) * 0.22
            album_h = (page_h - 2*margin) - margin_h - 4*mm
            album_x = margin
            album_y = margin + margin_h + 4*mm
            album_w = page_w - 2*margin
            mzone_x = margin
            mzone_y = margin
            mzone_w = page_w - 2*margin
            mzone_h = margin_h
        else:  # 'end' : pas de zone marge sur la page
            album_x = margin
            album_y = margin
            album_w = page_w - 2*margin
            album_h = page_h - 2*margin
            mzone_x = mzone_y = mzone_w = mzone_h = 0

        # Dessine les photos principales
        boxes = _grid_layout(len(items_chunk), album_x, album_y, album_w, album_h)
        for box, item in zip(boxes, items_chunk):
            _draw_in_box(item, *box)

        # ── Mini-carte du lieu (Brief 06 §5.3) ─────────────────────
        minimap_h_mm = 0
        if show_minimaps and margin_pos != 'end' and mzone_w > 0:
            section_coords = _section_coords_for_chunk(items_chunk)
            if section_coords:
                if margin_pos in ('right', 'left'):
                    mm_w_mm = (mzone_w / mm) - 4
                    mm_h_mm = min(mm_w_mm, 35)
                else:
                    mm_h_mm = (mzone_h / mm) - 4
                    mm_w_mm = min(mm_h_mm * 1.3, 50)
                mm_w_px = min(int(mm_w_mm * 5), 512)
                mm_h_px = min(int(mm_h_mm * 5), 512)
                zoom = section_coords.get('zoom', 12)
                map_png = _fetch_staticmap_png(
                    section_coords['lat'], section_coords['lng'],
                    zoom, mm_w_px, mm_h_px,
                    markers=[(section_coords['lat'], section_coords['lng'])]
                )
                if map_png:
                    try:
                        if margin_pos == 'right':
                            mm_x = mzone_x + 2*mm
                            mm_y = mzone_y + mzone_h - mm_h_mm * mm - 2*mm
                        elif margin_pos == 'left':
                            mm_x = mzone_x + (mzone_w - mm_w_mm * mm) / 2
                            mm_y = mzone_y + mzone_h - mm_h_mm * mm - 2*mm
                        else:
                            mm_x = mzone_x + 2*mm
                            mm_y = mzone_y + (mzone_h - mm_h_mm * mm) / 2
                        pdf.drawImage(ImageReader(io.BytesIO(map_png)),
                                      mm_x, mm_y,
                                      width=mm_w_mm * mm, height=mm_h_mm * mm,
                                      mask='auto')
                        pdf.setStrokeColorRGB(0.88, 0.85, 0.80)
                        pdf.setLineWidth(0.3)
                        pdf.rect(mm_x, mm_y, mm_w_mm * mm, mm_h_mm * mm, fill=0, stroke=1)
                        if section_coords.get('label'):
                            pdf.setFont('Helvetica', 6)
                            pdf.setFillColorRGB(0.42, 0.41, 0.38)
                            pdf.drawCentredString(mm_x + (mm_w_mm * mm) / 2,
                                                  mm_y - 3*mm,
                                                  section_coords['label'][:30].upper())
                        minimap_h_mm = mm_h_mm + 6
                    except Exception as e:
                        log.warning("PDF minimap draw fail: %s", e)

        # Dessine la zone marge si applicable
        if margin_pos != 'end' and mzone_w > 0:
            margins_for_this_page = _take_margins(margin_per_page)
            if margins_for_this_page:
                # Petit liseré pour démarquer
                pdf.setStrokeColorRGB(0.88, 0.85, 0.80)
                pdf.setDash(2, 2)
                pdf.setLineWidth(0.4)
                if margin_pos == 'right':
                    pdf.line(mzone_x - 2*mm, mzone_y, mzone_x - 2*mm, mzone_y + mzone_h)
                elif margin_pos == 'left':
                    pdf.line(mzone_x + mzone_w + 2*mm, mzone_y,
                             mzone_x + mzone_w + 2*mm, mzone_y + mzone_h)
                else:
                    pdf.line(mzone_x, mzone_y + mzone_h + 2*mm,
                             mzone_x + mzone_w, mzone_y + mzone_h + 2*mm)
                pdf.setDash()
                _draw_margin_zone(margins_for_this_page,
                                  mzone_x, mzone_y, mzone_w, mzone_h,
                                  "NOTES EN MARGE")

        pdf.showPage()

    # ─ Itere sur les pages principales par chunks de n_per_page ────
    chunks = [main_filtered[i:i + n_per_page]
              for i in range(0, len(main_filtered), n_per_page)] or [[]]
    for chunk in chunks:
        if chunk:
            _draw_composite_page(chunk)

    # Marges restantes : si margin_pos='end' OU s'il reste des marges non placees
    remaining_margins = margin_items[margin_idx:] if margin_pos != 'end' else pages_data['margin']
    if remaining_margins:
        # Page de garde
        pdf.setFillColorRGB(0.98, 0.972, 0.957)
        pdf.rect(0, 0, page_w, page_h, fill=1, stroke=0)
        pdf.setFont('Times-Italic', 28)
        pdf.setFillColorRGB(0.110, 0.102, 0.090)
        pdf.drawCentredString(page_w / 2, page_h / 2, "Notes en marge")
        pdf.setFont('Helvetica', 9)
        pdf.setFillColorRGB(0.64, 0.611, 0.572)
        pdf.drawCentredString(page_w / 2, page_h / 2 - 30,
                              "PHOTOS DE CONTEXTE · LIEUX · BILLETS")
        pdf.showPage()

        per_page = 4
        for chunk_start in range(0, len(remaining_margins), per_page):
            chunk = remaining_margins[chunk_start:chunk_start + per_page]
            pdf.setFillColorRGB(0.98, 0.972, 0.957)
            pdf.rect(0, 0, page_w, page_h, fill=1, stroke=0)
            cell_w = (page_w - 3 * margin) / 2
            cell_h = (page_h - 3 * margin) / 2
            for i, m in enumerate(chunk):
                col, row = i % 2, i // 2
                cx = margin + col * (cell_w + margin)
                cy = margin + (1 - row) * (cell_h + margin)
                if m.get('photo_path'):
                    try:
                        ph = os.path.join(UPLOAD_DIR, m['photo_path'])
                        img = ImageReader(ph)
                        iw, ih = img.getSize()
                        ratio = min(cell_w / iw, (cell_h - 8*mm) / ih)
                        dw, dh = iw * ratio, ih * ratio
                        pdf.drawImage(img, cx + (cell_w - dw)/2, cy + 8*mm,
                                      width=dw, height=dh, mask='auto')
                    except Exception:
                        pass
                if m.get('caption') or m.get('text_content'):
                    pdf.setFont('Times-Italic', 9)
                    pdf.setFillColorRGB(0.24, 0.227, 0.207)
                    _wrap_text(pdf, m.get('caption') or m.get('text_content'),
                               cx + cell_w/2, cy + 3*mm,
                               max_width=cell_w, line_height=11, max_lines=3)
            pdf.showPage()

    # ─ Page de fin ─────────────────────────────────────────────────
    pdf.setFillColorRGB(0.98, 0.972, 0.957)
    pdf.rect(0, 0, page_w, page_h, fill=1, stroke=0)
    pdf.setFont('Times-Italic', 14)
    pdf.setFillColorRGB(0.42, 0.41, 0.38)
    pdf.drawCentredString(page_w / 2, page_h / 2, "Fin")
    pdf.setFont('Helvetica', 8)
    pdf.setFillColorRGB(0.64, 0.611, 0.572)
    pdf.drawCentredString(page_w / 2, margin, "NOTRE HISTOIRE · histoire.aqgk.fr")

    pdf.showPage()
    pdf.save()
    buf.seek(0)

    safe_title = ''.join(ch if ch.isalnum() or ch in ('-', '_') else '_'
                         for ch in c['title'])[:40]
    fname = f"{safe_title}_{fmt}.pdf"
    from flask import Response
    return Response(buf.getvalue(),
        mimetype='application/pdf',
        headers={'Content-Disposition': f'attachment; filename="{fname}"'})


def _wrap_text(pdf, text, cx, cy, max_width, line_height=14, max_lines=99):
    """Affichage centre multi-ligne texte (wrap basique sur largeur)."""
    from reportlab.pdfbase.pdfmetrics import stringWidth
    font_name = pdf._fontname
    font_size = pdf._fontsize
    words = (text or '').split()
    lines, cur = [], []
    for w in words:
        test = ' '.join(cur + [w])
        if stringWidth(test, font_name, font_size) <= max_width:
            cur.append(w)
        else:
            if cur: lines.append(' '.join(cur))
            cur = [w]
    if cur: lines.append(' '.join(cur))
    lines = lines[:max_lines]
    total_h = len(lines) * line_height
    y = cy + total_h / 2
    for line in lines:
        pdf.drawCentredString(cx, y, line)
        y -= line_height


# ══════════════════════════════════════════════════════════════════════
#                         v1.2 — ALBUM
# ══════════════════════════════════════════════════════════════════════

def _norm_ts(ts):
    """v3.4 : normalise un timestamp en cle triable 'YYYY-MM-DD HH:MM:SS'.
    Neutralise les formats mixtes qui cassaient le tri chronologique par
    comparaison de chaines : 'T' vs espace ('T' > ' ' en ASCII), suffixe 'Z'
    et millisecondes des dates ISO envoyees par le client (exifr)."""
    if not ts:
        return ''
    s = str(ts).strip().replace('T', ' ')
    if s.endswith('Z'):
        s = s[:-1]
    return s[:19]


def _page_chrono_key(p):
    """Cle de tri chronologique d'une page d'album (photo > video > ajout)."""
    ts = _norm_ts(p.get('photo_taken_at') or p.get('video_taken_at')
                  or p.get('created_at'))
    # Pages sans date -> en fin, ordre stable par position/id
    return (ts == '', ts, p.get('position') or 0, p.get('id') or 0)


def _carnet_pages(carnet_id, sort_mode='chrono'):
    """
    Retourne les pages d'un carnet selon le sort_mode :
    - 'chrono' (default) : tri par date EXIF/ajout (normalise en Python,
      les formats de taken_at etant heterogenes -> pas de tri fiable en SQL)
    - 'manual' : tri par position (drag & drop)
    Renvoie un dict avec deux listes : 'main' (album) et 'margin' (notes en marge).
    """
    order_by = "ap.position ASC, ap.id ASC"
    rows = query(f"""
        SELECT ap.*,
               p.file_path AS photo_path, p.thumb_path AS photo_thumb,
               p.width AS photo_width, p.height AS photo_height,
               p.taken_at AS photo_taken_at,
               p.taken_at_source AS photo_taken_at_source,
               p.orig_filename AS photo_orig_filename,
               p.mid_path AS photo_mid,
               p.gps_lat AS photo_gps_lat, p.gps_lng AS photo_gps_lng,
               p.address_full AS photo_address_full,
               p.country AS photo_country, p.state AS photo_state,
               p.city_name AS photo_city, p.road AS photo_road,
               v.file_path AS video_path, v.poster_path AS video_poster,
               v.duration_s AS video_duration, v.scan_token AS video_token,
               v.taken_at AS video_taken_at,
               u.display_name AS added_by_name
        FROM album_pages ap
        LEFT JOIN photos p ON p.id = ap.photo_id
        LEFT JOIN videos v ON v.id = ap.video_id
        LEFT JOIN users u ON u.id = ap.added_by
        WHERE ap.carnet_id = ?
        ORDER BY {order_by}
    """, (carnet_id,))
    pages = [dict(r) for r in rows]
    # v5.18 : tri chrono INCONDITIONNEL — la chronologie est l'unique ordre
    # de verite (les re-datations v5.13 gouvernent, jamais une position figee)
    pages.sort(key=_page_chrono_key)
    # v5.11 : les pages en corbeille (is_hidden=1) sortent de l'album, de
    # l'apercu et du livre — mais restent listees a part pour la restauration.
    hidden = [p for p in pages if p.get('is_hidden')]
    pages = [p for p in pages if not p.get('is_hidden')]
    main = [p for p in pages if not p.get('is_margin')]
    margin = [p for p in pages if p.get('is_margin')]

    # v2.3 : organisation par sections (cas A/B/C)
    sections = query("""
        SELECT * FROM album_sections WHERE carnet_id=?
        ORDER BY position ASC, id ASC
    """, (carnet_id,))
    sections = [dict(s) for s in sections]
    sec_by_id = {s['id']: s for s in sections}
    # Group sections par level 1
    level1 = [s for s in sections if s['level'] == 1]
    structured = []
    for s1 in level1:
        children = [s for s in sections if s['parent_section_id'] == s1['id']]
        children.sort(key=lambda x: x['position'])
        sub = []
        for c in children:
            child_pages = [p for p in main if p.get('section_id') == c['id']]
            sub.append({'section': c, 'pages': child_pages})
        # Pages directement rattachees au level 1 (rare)
        direct = [p for p in main if p.get('section_id') == s1['id']]
        structured.append({'section': s1, 'subsections': sub, 'pages': direct})
    # Pages sans section (taken_at manquant) -> categorie speciale
    orphans = [p for p in main if not p.get('section_id')]

    return {
        'main': main, 'margin': margin, 'all': pages,
        'structured': structured, 'orphans': orphans,
        'hidden': hidden,
    }


def _next_page_position(carnet_id):
    r = query(
        "SELECT COALESCE(MAX(position), -1) + 1 AS next FROM album_pages WHERE carnet_id=?",
        (carnet_id,), one=True
    )
    return r['next'] if r else 0


# ══════════════════════════════════════════════════════════════════════
#   Brief 08 §3 — Reverse geocoding (coords → Pays / Departement / Ville / Rue)
# ══════════════════════════════════════════════════════════════════════
_GEO_UA = 'Notre-Histoire/1.0 (+arthur.kembellec@gmail.com)'


def _geo_cache_key(lat, lng):
    """Cle de cache : coords arrondies a 4 decimales (~11m) pour mutualiser
    le geocodage entre photos prises au meme endroit."""
    try:
        return f"{round(float(lat), 4):.4f},{round(float(lng), 4):.4f}"
    except (TypeError, ValueError):
        return None


def _forward_geocode(q, limit=6):
    """v4.1 Reveries : recherche de lieu (geocodage direct Nominatim).
    Best effort, renvoie [{label, lat, lng}] (max `limit`)."""
    import json as _json
    import urllib.parse as _up
    if not q or len(q.strip()) < 2:
        return []
    url = ("https://nominatim.openstreetmap.org/search"
           f"?format=jsonv2&q={_up.quote(q.strip())}&limit={int(limit)}"
           "&accept-language=fr")
    try:
        req = urllib.request.Request(url, headers={'User-Agent': _GEO_UA})
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = _json.loads(resp.read())
        out = []
        for d in data:
            try:
                out.append({
                    'label': d.get('display_name') or '',
                    'lat': float(d['lat']), 'lng': float(d['lon']),
                })
            except (KeyError, TypeError, ValueError):
                continue
        return out
    except Exception as e:
        log.warning("forward geocode fail %r: %s", q, e)
        return []


@app.route('/geo/search')
@couple_required
def geo_search():
    """Recherche de lieu pour la carte des reveries."""
    q = request.args.get('q') or ''
    return jsonify({'ok': True, 'results': _forward_geocode(q)})


def _route_cache_key(coords, profile):
    """Cle stable : le profil + les coordonnees arrondies a 5 decimales
    (~1 m — deux traces du meme parcours tombent sur la meme cle)."""
    brut = profile + '|' + ';'.join(f"{lat:.5f},{lng:.5f}" for lat, lng in coords)
    return hashlib.sha1(brut.encode('utf-8')).hexdigest()


def _route_cache_get(coords, profile):
    import json as _json
    try:
        r = query("SELECT duration_s, distance_m, legs, geometry FROM route_cache "
                  "WHERE key=?", (_route_cache_key(coords, profile),), one=True)
        if not r:
            return None
        return {'duration_s': r['duration_s'], 'distance_m': r['distance_m'],
                'legs': _json.loads(r['legs'] or '[]'),
                'geometry': _json.loads(r['geometry'] or '[]')}
    except Exception as e:
        log.warning("route_cache lecture: %s", e)   # repli : on rappellera OSRM
        return None


def _route_cache_put(coords, profile, route):
    import json as _json
    try:
        execute("INSERT OR REPLACE INTO route_cache "
                "(key, profile, duration_s, distance_m, legs, geometry) "
                "VALUES (?,?,?,?,?,?)",
                (_route_cache_key(coords, profile), profile,
                 route.get('duration_s'), route.get('distance_m'),
                 _json.dumps(route.get('legs') or []),
                 _json.dumps(route.get('geometry') or [])))
    except Exception as e:
        log.warning("route_cache ecriture: %s", e)


def _osrm_route(coords, profile='car'):
    """v4.3 : itineraire reel via OSRM (instances FOSSGIS, fair use).
    coords = [(lat, lng), ...] ordonnees. profile 'car' ou 'foot'.
    Renvoie {duration_s, distance_m, legs, geometry:[[lat,lng],...]} ou None."""
    import json as _json
    if not coords or len(coords) < 2:
        return None
    profile = 'foot' if profile == 'foot' else 'car'
    path = ';'.join(f"{lng:.6f},{lat:.6f}" for lat, lng in coords)
    url = (f"https://routing.openstreetmap.de/routed-{profile}/route/v1/driving/"
           f"{path}?overview=full&geometries=geojson&steps=false")
    try:
        req = urllib.request.Request(url, headers={'User-Agent': _GEO_UA})
        with urllib.request.urlopen(req, timeout=9) as resp:
            data = _json.loads(resp.read())
        if data.get('code') != 'Ok' or not data.get('routes'):
            return None
        r = data['routes'][0]
        return {
            'duration_s': round(r.get('duration') or 0),
            'distance_m': round(r.get('distance') or 0),
            'legs': [{'duration_s': round(l.get('duration') or 0),
                      'distance_m': round(l.get('distance') or 0)}
                     for l in (r.get('legs') or [])],
            'geometry': [[c[1], c[0]] for c in
                         (r.get('geometry') or {}).get('coordinates') or []],
        }
    except Exception as e:
        log.warning("osrm route fail (%s): %s", profile, e)
        return None


@app.route('/geo/route')
@couple_required
def geo_route():
    """Itineraire du parcours reverie : ?coords=lat,lng;lat,lng&profile=car|foot"""
    raw = (request.args.get('coords') or '').strip()
    profile = request.args.get('profile') or 'car'
    coords = []
    for part in raw.split(';'):
        bits = part.split(',')
        if len(bits) != 2:
            continue
        lat, lng = _safe_float(bits[0]), _safe_float(bits[1])
        if lat is None or lng is None:
            continue
        coords.append((lat, lng))
    if len(coords) < 2 or len(coords) > 25:
        return jsonify({'ok': False, 'error': '2 a 25 etapes'}), 400
    # v5.2 : cache disque. Sans lui, chaque affichage de la reverie rejoue un
    # appel par bloc sur un service public en fair use — et la page attend.
    cached = _route_cache_get(coords, profile)
    if cached:
        return jsonify({'ok': True, 'cached': True, **cached})
    route = _osrm_route(coords, profile)
    if not route:
        return jsonify({'ok': False, 'error': 'itineraire indisponible'})
    _route_cache_put(coords, profile, route)
    return jsonify({'ok': True, **route})


@app.route('/geo/sleep')
@couple_required
def geo_sleep():
    """v4.3 : calque « Ou dormir » — hebergements OSM (Overpass) dans la zone.
    ?bbox=sud,ouest,nord,est — zone limitee pour rester rapide et poli."""
    import json as _json
    raw = (request.args.get('bbox') or '').split(',')
    if len(raw) != 4:
        return jsonify({'ok': False, 'error': 'bbox invalide'}), 400
    try:
        s, w, n, e = (float(x) for x in raw)
    except ValueError:
        return jsonify({'ok': False, 'error': 'bbox invalide'}), 400
    if not (n > s and e > w) or (n - s) > 1.2 or (e - w) > 1.6:
        return jsonify({'ok': False, 'error': 'Zoome un peu pour chercher ou dormir'})
    kinds = 'hotel|guest_house|hostel|chalet|camp_site|alpine_hut|wilderness_hut|apartment|motel'
    q = (f'[out:json][timeout:10];('
         f'node[tourism~"^({kinds})$"]({s},{w},{n},{e});'
         f'way[tourism~"^({kinds})$"]({s},{w},{n},{e});'
         f');out center 40;')
    # v4.4.1 : instances de repli — overpass-api.de refuse parfois les IP cloud
    data = None
    for endpoint in ('https://overpass-api.de/api/interpreter',
                     'https://overpass.kumi.systems/api/interpreter',
                     'https://overpass.private.coffee/api/interpreter'):
        try:
            req = urllib.request.Request(
                endpoint,
                data=('data=' + urllib.parse.quote(q)).encode(),
                headers={'User-Agent': _GEO_UA,
                         'Content-Type': 'application/x-www-form-urlencoded'})
            with urllib.request.urlopen(req, timeout=12) as resp:
                data = _json.loads(resp.read())
            break
        except Exception as e:
            log.warning("overpass %s KO: %s", endpoint, e)
            continue
    try:
        if data is None:
            raise RuntimeError('toutes les instances Overpass KO')
        out = []
        for el in (data.get('elements') or [])[:40]:
            lat = el.get('lat') or (el.get('center') or {}).get('lat')
            lng = el.get('lon') or (el.get('center') or {}).get('lon')
            if lat is None or lng is None:
                continue
            tags = el.get('tags') or {}
            out.append({
                'lat': lat, 'lng': lng,
                'name': tags.get('name') or '',
                'kind': tags.get('tourism') or '',
            })
        return jsonify({'ok': True, 'results': out})
    except Exception as e:
        log.warning("overpass sleep fail: %s", e)
        return jsonify({'ok': False, 'error': 'recherche indisponible'})


def _reverse_geocode(lat, lng):
    """Resoud des coordonnees GPS en {country, state, city, road, full}.

    - Cache disque dans la table geo_cache (cle = lat/lng arrondi 4 dec).
    - Best effort : retourne {} en cas d'erreur reseau ou hors-ligne.
    - Respecte la policy Nominatim (User-Agent identifie, 1 req/s max
      assure par le cache + l'usage par lot a l'upload).
    """
    key = _geo_cache_key(lat, lng)
    if not key:
        return {}
    try:
        r = query("SELECT country, state, city, road, full FROM geo_cache WHERE key=?",
                  (key,), one=True)
        if r:
            return {'country': r['country'] or '', 'state': r['state'] or '',
                    'city': r['city'] or '', 'road': r['road'] or '',
                    'full': r['full'] or ''}
    except Exception as e:
        log.debug("geo_cache lookup fail: %s", e)
    # Lookup Nominatim
    url = ("https://nominatim.openstreetmap.org/reverse"
           f"?format=jsonv2&lat={lat}&lon={lng}&zoom=14"
           "&addressdetails=1&accept-language=fr")
    try:
        req = urllib.request.Request(url, headers={'User-Agent': _GEO_UA})
        with urllib.request.urlopen(req, timeout=6) as resp:
            raw = resp.read()
        import json as _json
        data = _json.loads(raw)
        addr = data.get('address') or {}
        country = addr.get('country') or ''
        state = (addr.get('state') or addr.get('region')
                 or addr.get('county') or '')
        city = (addr.get('city') or addr.get('town') or addr.get('village')
                or addr.get('hamlet') or addr.get('locality')
                or addr.get('suburb') or addr.get('municipality') or '')
        road = (addr.get('road') or addr.get('pedestrian')
                or addr.get('footway') or addr.get('path') or '')
        # Reconstitue une adresse lisible : Rue · Ville · Departement · Pays
        parts = [p for p in [road, city, state, country] if p]
        full = ' · '.join(parts)
        try:
            execute("INSERT OR REPLACE INTO geo_cache "
                    "(key, country, state, city, road, full) VALUES (?,?,?,?,?,?)",
                    (key, country, state, city, road, full))
        except Exception as e:
            log.debug("geo_cache write fail: %s", e)
        return {'country': country, 'state': state, 'city': city,
                'road': road, 'full': full}
    except Exception as e:
        log.info("reverse_geocode fail (%s,%s): %s", lat, lng, e)
        # Cache l'echec brievement (vide) pour eviter de marteler
        try:
            execute("INSERT OR IGNORE INTO geo_cache "
                    "(key, country, state, city, road, full) VALUES (?,?,?,?,?,?)",
                    (key, '', '', '', '', ''))
        except Exception:
            pass
        return {}


def _enrich_photo_geo(photo_id, lat, lng):
    """Reverse-geocode + UPDATE photos. Best effort, silencieux si echec."""
    if lat is None or lng is None:
        return
    try:
        info = _reverse_geocode(lat, lng)
        if not info:
            return
        execute("UPDATE photos SET country=?, state=?, city_name=?, road=?, "
                "address_full=? WHERE id=?",
                (info.get('country') or '', info.get('state') or '',
                 info.get('city') or '', info.get('road') or '',
                 info.get('full') or '', photo_id))
    except Exception as e:
        log.debug("enrich photo geo fail: %s", e)


def _gps_dms_to_dd(dms, ref):
    """Convertit DMS rationals + ref ('N','S','E','W') en degres decimaux."""
    try:
        if not dms or len(dms) < 3:
            return None
        def _r(v):
            if hasattr(v, 'numerator'): return v.numerator / max(v.denominator, 1)
            if isinstance(v, tuple) and len(v) == 2: return v[0] / max(v[1], 1)
            return float(v)
        d, m, s = _r(dms[0]), _r(dms[1]), _r(dms[2])
        dd = d + m / 60.0 + s / 3600.0
        if ref in ('S', 'W', b'S', b'W'):
            dd = -dd
        return round(dd, 7)
    except Exception:
        return None


def _date_from_filename(filename):
    """v5.7 — Devine la date de prise depuis le NOM du fichier quand l'EXIF
    est absent (WhatsApp, Instagram et les apps de rencontre strippent les
    metadonnees). Retourne un ISO 'YYYY-MM-DDTHH:MM:SS' ou None.

    Motifs reconnus (les plus courants du terrain) :
      IMG-20260712-WA0003     WhatsApp (date seule -> heure neutre 12:00)
      PXL_20260712_103340123  Google Pixel (date + heure)
      IMG_20260712_103340     Android / GoPro ; idem VID_/PANO_
      20260712_103340         Samsung
      Screenshot_2026-07-12-10-33-40 / Screenshot 2026-07-12 at 10.33.40
      2026-07-12 10.33.40     export divers
    Une date sans heure recoit 12:00:00 (neutre : reste dans la bonne
    journee sans pretendre a une heure precise).
    """
    import re as _re
    if not filename:
        return None
    name = os.path.basename(str(filename))

    def _valide(y, mo, d, h=12, mi=0, s=0):
        try:
            dt = datetime(int(y), int(mo), int(d), int(h), int(mi), int(s))
        except ValueError:
            return None
        if not (2000 <= dt.year <= 2100):
            return None
        # Une date de prise dans le futur est un faux positif (suite de
        # chiffres qui ressemble a une date), pas une date.
        if dt > datetime.now() + timedelta(days=1):
            return None
        return dt.isoformat(timespec='seconds')

    # 1) Date + heure compactes : PXL_/IMG_/VID_/20260712_103340
    m = _re.search(r'(20\d{2})(\d{2})(\d{2})[_-]?(\d{2})(\d{2})(\d{2})', name)
    if m:
        iso = _valide(*m.groups())
        if iso:
            return iso
    # 2) Date + heure avec separateurs : Screenshot_2026-07-12-10-33-40,
    #    'Screenshot 2026-07-12 at 10.33.40', '2026-07-12 10.33.40'
    m = _re.search(r'(20\d{2})-(\d{2})-(\d{2})[ _-]+(?:at[ _])?(\d{2})[.\-h:](\d{2})[.\-m:]?(\d{2})?', name)
    if m:
        y, mo, d, h, mi, s = m.groups()
        iso = _valide(y, mo, d, h, mi, s or 0)
        if iso:
            return iso
    # 3) Date compacte seule : IMG-20260712-WA0003, 20260712.jpg
    m = _re.search(r'(?:^|[^\d])(20\d{2})(\d{2})(\d{2})(?:[^\d]|$)', name)
    if m:
        iso = _valide(*m.groups())
        if iso:
            return iso
    # 4) Date ISO seule : 2026-07-12
    m = _re.search(r'(20\d{2})-(\d{2})-(\d{2})', name)
    if m:
        iso = _valide(*m.groups())
        if iso:
            return iso
    return None


def _save_uploaded_photo(file, couple_id):
    """
    Sauvegarde une photo uploadee :
    - Lit EXIF AVANT compression : DateTimeOriginal + GPS + Orientation
    - Resize a 2000px max (cote long), qualite 85
    - Genere un thumbnail 400px (qualite 70)
    - Renomme en token random pour eviter collision
    Retourne dict {file_path, thumb_path, width, height, taken_at, gps_lat, gps_lng}.
    """
    img = Image.open(file.stream)

    taken_at = None
    gps_lat = None
    gps_lng = None
    try:
        exif = img._getexif() or {}

        # Orientation
        orient_key = next((k for k, v in ExifTags.TAGS.items() if v == 'Orientation'), None)
        if orient_key and orient_key in exif:
            o = exif[orient_key]
            if o == 3: img = img.rotate(180, expand=True)
            elif o == 6: img = img.rotate(270, expand=True)
            elif o == 8: img = img.rotate(90, expand=True)

        # Date prise (plusieurs cles selon source)
        for key_name in ('DateTimeOriginal', 'CreateDate', 'DateTime'):
            key = next((k for k, v in ExifTags.TAGS.items() if v == key_name), None)
            if key and key in exif and not taken_at:
                try:
                    taken_at = datetime.strptime(exif[key], '%Y:%m:%d %H:%M:%S').isoformat()
                except Exception:
                    pass

        # GPS (tag 34853 = GPSInfo)
        gps_info = exif.get(34853)
        if gps_info:
            lat_ref = gps_info.get(1)
            lat_dms = gps_info.get(2)
            lng_ref = gps_info.get(3)
            lng_dms = gps_info.get(4)
            if lat_dms and lng_dms:
                gps_lat = _gps_dms_to_dd(lat_dms, lat_ref)
                gps_lng = _gps_dms_to_dd(lng_dms, lng_ref)
    except Exception as e:
        log.debug("EXIF read fail: %s", e)

    # Convert RGBA / P -> RGB pour JPEG
    if img.mode in ('RGBA', 'LA', 'P'):
        bg = Image.new('RGB', img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[-1] if img.mode in ('RGBA', 'LA') else None)
        img = bg
    elif img.mode != 'RGB':
        img = img.convert('RGB')

    # Resize image principale (2000px cote long)
    img.thumbnail((2000, 2000), Image.Resampling.LANCZOS)
    w, h = img.size

    # Stockage : /app/data/uploads/<couple_id>/<token>.jpg
    couple_dir = os.path.join(UPLOAD_DIR, str(couple_id))
    os.makedirs(couple_dir, exist_ok=True)
    token = secrets.token_urlsafe(12)
    fname = f"{token}.jpg"
    fpath = os.path.join(couple_dir, fname)
    img.save(fpath, 'JPEG', quality=85, optimize=True)

    # Thumbnail 400px
    thumb = img.copy()
    thumb.thumbnail((400, 400), Image.Resampling.LANCZOS)
    thumb_fname = f"{token}_t.jpg"
    thumb_fpath = os.path.join(couple_dir, thumb_fname)
    thumb.save(thumb_fpath, 'JPEG', quality=72, optimize=True)

    # v5.7 : taille intermediaire 1024px (photo hero de scene)
    mid = img.copy()
    mid.thumbnail((1024, 1024), Image.Resampling.LANCZOS)
    mid_fname = f"{token}_m.jpg"
    mid_fpath = os.path.join(couple_dir, mid_fname)
    mid.save(mid_fpath, 'JPEG', quality=80, optimize=True)

    rel_file = f"{couple_id}/{fname}"
    rel_thumb = f"{couple_id}/{thumb_fname}"
    rel_mid = f"{couple_id}/{mid_fname}"
    return {
        'file_path': rel_file,
        'thumb_path': rel_thumb,
        'mid_path': rel_mid,
        'width': w, 'height': h,
        'taken_at': taken_at,
        'gps_lat': gps_lat,
        'gps_lng': gps_lng,
    }


def _ensure_mid(photo_id, file_path, mid_path):
    """v5.7 — Garantit la version 1024px d'une photo EXISTANTE (les photos
    d'avant v5.7 n'ont pas de mid). Generation paresseuse depuis l'original
    2000px, une seule fois, au premier affichage en hero. Retourne le chemin
    relatif du mid, ou None si la generation echoue (le template retombe
    alors sur le thumb — et le log le dit, regle R4)."""
    if mid_path:
        return mid_path
    if not file_path:
        return None
    src = os.path.join(UPLOAD_DIR, file_path)
    if not os.path.exists(src):
        log.warning("_ensure_mid: original absent (%s)", file_path)
        return None
    try:
        img = Image.open(src)
        img.thumbnail((1024, 1024), Image.Resampling.LANCZOS)
        if img.mode != 'RGB':
            img = img.convert('RGB')
        base, _ = os.path.splitext(file_path)
        rel_mid = f"{base}_m.jpg"
        img.save(os.path.join(UPLOAD_DIR, rel_mid), 'JPEG', quality=80, optimize=True)
        execute("UPDATE photos SET mid_path=? WHERE id=?", (rel_mid, photo_id))
        return rel_mid
    except Exception as e:
        log.warning("_ensure_mid: echec pour photo %s : %s", photo_id, e)
        return None


def _deg_to_dms_rational(deg):
    """Convertit un float degrees en rationals EXIF DMS."""
    deg_abs = abs(deg)
    d = int(deg_abs)
    m_full = (deg_abs - d) * 60
    m = int(m_full)
    s = (m_full - m) * 60
    return ((d, 1), (m, 1), (int(round(s * 100)), 100))


def _inject_exif_to_jpeg(jpeg_path, taken_at_iso=None, gps_lat=None, gps_lng=None):
    """v1.2.4 — Reinjecte les EXIF DateTimeOriginal + GPS dans le JPEG
    apres la compression Pillow (qui les supprime). Silencieux en cas d'erreur."""
    try:
        import piexif
    except ImportError:
        return
    try:
        try:
            exif_dict = piexif.load(jpeg_path)
        except Exception:
            exif_dict = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}, "thumbnail": None}

        if taken_at_iso:
            try:
                # ISO 'YYYY-MM-DDTHH:MM:SS[.fff]' -> EXIF 'YYYY:MM:DD HH:MM:SS'
                s = str(taken_at_iso).replace('T', ' ').split('.')[0]
                # Remplace seulement les '-' de la date (pas ceux d'eventuel TZ apres seconds)
                if len(s) >= 10:
                    s = s[:10].replace('-', ':') + s[10:]
                if len(s) >= 19:
                    s = s[:19]
                b = s.encode('ascii')
                exif_dict.setdefault('Exif', {})[piexif.ExifIFD.DateTimeOriginal] = b
                exif_dict['Exif'][piexif.ExifIFD.DateTimeDigitized] = b
                exif_dict.setdefault('0th', {})[piexif.ImageIFD.DateTime] = b
            except Exception as e:
                log.debug("EXIF date inject skip: %s", e)

        if gps_lat is not None and gps_lng is not None:
            try:
                exif_dict.setdefault('GPS', {})
                exif_dict['GPS'][piexif.GPSIFD.GPSLatitudeRef]  = b'N' if gps_lat >= 0 else b'S'
                exif_dict['GPS'][piexif.GPSIFD.GPSLatitude]     = _deg_to_dms_rational(gps_lat)
                exif_dict['GPS'][piexif.GPSIFD.GPSLongitudeRef] = b'E' if gps_lng >= 0 else b'W'
                exif_dict['GPS'][piexif.GPSIFD.GPSLongitude]    = _deg_to_dms_rational(gps_lng)
                exif_dict['GPS'][piexif.GPSIFD.GPSVersionID]    = (2, 3, 0, 0)
            except Exception as e:
                log.debug("EXIF GPS inject skip: %s", e)

        if any(exif_dict.get(k) for k in ('0th', 'Exif', 'GPS')):
            piexif.insert(piexif.dump(exif_dict), jpeg_path)
    except Exception as e:
        log.warning("EXIF reinjection failed for %s: %s", jpeg_path, e)


@app.template_filter('doux_label')
def _doux_label(s):
    """v4 : adoucit les libelles de section pour l'affichage album.
    - retire les coordonnees GPS brutes (45.527,2.773) laissees quand le
      reverse-geocoding n'a pas (encore) de nom de lieu ;
    - replie les plages horaires identiques (15:53 -> 15:53 => 15:53) ;
    - accorde "N photo(s)" en francais.
    Ne modifie jamais la donnee en base, uniquement le rendu."""
    import re
    if not s:
        return s
    s = str(s)
    s = re.sub(r'-?\d{1,3}\.\d{2,},\s*-?\d{1,3}\.\d{2,}', '', s)
    s = re.sub(r'(\d{1,2}:\d{2})\s*\u2192\s*\1', r'\1', s)
    s = re.sub(r'\b1 photo\(s\)', '1 photo', s)
    s = re.sub(r'\b(\d+) photo\(s\)', r'\1 photos', s)
    # nettoyage des separateurs orphelins laisses par le retrait des coords
    s = re.sub(r'\s*\u00b7\s*(\u00b7|$)', r' \1', s)
    s = re.sub(r'(\s*[\u00b7,]\s*)+', lambda m: ' \u00b7 ' if '\u00b7' in m.group(0) else ', ', s)
    s = re.sub(r'^[\s\u00b7,]+|[\s\u00b7,]+$', '', s)
    s = re.sub(r'\s{2,}', ' ', s)
    return s or 'Lieu inconnu'


@app.route('/carnet/<int:cid_carnet>/album')
@couple_required
def carnet_album(cid_carnet):
    """Mode edition album : photos, captions, blocs texte, notes en marge."""
    c = _get_carnet_or_404(cid_carnet)
    sort_mode = c.get('sort_mode') or 'chrono'
    pages = _carnet_pages(cid_carnet, sort_mode=sort_mode)
    # v5.6 : la carte du carnet montre la MEME chose que celle de la reverie —
    # les etapes preparees, avec l'epingle deja choisie la-bas, plus les photos.
    geo_etapes = []
    for r_ in query("""
        SELECT id, title, pin_kind, planned_day, geo_lat, geo_lng
        FROM carnet_items
        WHERE carnet_id=? AND kind='location'
          AND geo_lat IS NOT NULL AND geo_lng IS NOT NULL
          AND deleted_at IS NULL
        ORDER BY planned_day, position
    """, (cid_carnet,)):
        r_ = dict(r_)
        geo_etapes.append({
            'item_id': r_['id'],
            'lat': r_['geo_lat'], 'lng': r_['geo_lng'],
            'pin_kind': r_['pin_kind'] or '',
            'planned_day': r_['planned_day'],
            'title': r_['title'] or 'Etape',
        })
    geo_photos = [p for p in pages['all']
                  if p.get('photo_gps_lat') is not None and p.get('photo_gps_lng') is not None]
    # v4.6 : etapes du planning du voyage, par date reelle (date_start + jour)
    etapes_by_day = {}
    etapes_all = []
    if c.get('date_start'):
        # v5.1 : les jours viennent de trajet_steps — une meme etape peut
        # figurer dans DEUX jours du squelette (nuit au gite : soir du J1 et
        # matin du J2). etapes_all reste dedoublonne (compteur + ancres).
        days_steps = _trajet_days(cid_carnet)
        ids_et = list(dict.fromkeys(iid for day in days_steps for iid in day))
        infos = {}
        if ids_et:
            ph_et = ','.join('?' * len(ids_et))
            for r_ in query(f"""
                SELECT id, title, pin_kind, planned_day, position FROM carnet_items
                WHERE carnet_id=? AND kind='location' AND id IN ({ph_et})
                  AND deleted_at IS NULL
            """, tuple([cid_carnet] + ids_et)):
                infos[r_['id']] = dict(r_)
        from datetime import date as _date, timedelta as _td
        try:
            d0 = _date.fromisoformat(str(c['date_start'])[:10])
        except ValueError:
            d0 = None
        vus_et = set()
        for k, day in enumerate(days_steps):
            for iid in day:
                base = infos.get(iid)
                if not base:
                    continue
                r_ = dict(base)
                r_['planned_day'] = k          # le jour de CETTE occurrence
                if iid not in vus_et:
                    vus_et.add(iid)
                    etapes_all.append(r_)
                if d0 is not None:
                    day_key = (d0 + _td(days=k)).isoformat()
                    r_['day'] = day_key
                    etapes_by_day.setdefault(day_key, []).append(r_)
    etape_by_id = {e['id']: e for e in etapes_all}
    # v4.2 : enrichir les notes ancrees (vignette + chronologie de la photo)
    anchors = {}
    anchor_ids = [p['anchor_page_id'] for p in pages['margin'] if p.get('anchor_page_id')]
    if anchor_ids:
        ph = ','.join('?' * len(anchor_ids))
        for r in query(f"""
            SELECT ap.id, p.thumb_path, p.taken_at, ap.caption
            FROM album_pages ap LEFT JOIN photos p ON p.id = ap.photo_id
            WHERE ap.id IN ({ph})
        """, tuple(anchor_ids)):
            anchors[r['id']] = dict(r)
    for p in pages['margin']:
        a = anchors.get(p.get('anchor_page_id'))
        if a:
            p['anchor_thumb'] = a.get('thumb_path')
            p['anchor_ts'] = _norm_ts(a.get('taken_at'))
        else:
            p['anchor_thumb'] = None
            p['anchor_ts'] = ''
        # v4.6 : ancre vers une etape -> jour de l'etape + libelle
        et = etape_by_id.get(p.get('anchor_item_id'))
        if et:
            p['anchor_etape'] = et
            if et.get('day'):
                p['anchor_ts'] = et['day'] + ' 00:00:01'
        else:
            p['anchor_etape'] = None
    # Tri du rail : une note ancree suit la chronologie de SA photo
    pages['margin'].sort(key=lambda p: (
        (p.get('anchor_ts') or _norm_ts(p.get('photo_taken_at')
         or p.get('video_taken_at') or p.get('created_at'))) == '',
        p.get('anchor_ts') or _norm_ts(p.get('photo_taken_at')
         or p.get('video_taken_at') or p.get('created_at')),
        p.get('id') or 0,
    ))
    # v3.4 : notes de marge groupees par jour (suivent la chronologie de l'album)
    margin_groups = []
    last_day = None
    for p in pages['margin']:
        ts = p.get('anchor_ts') or _norm_ts(p.get('photo_taken_at')
             or p.get('video_taken_at') or p.get('created_at'))
        day = ts[:10] if ts else ''
        if day != last_day or not margin_groups:
            margin_groups.append({
                'day': day,
                'label': _format_day_fr(day) if day else 'Sans date',
                'pages': [],
            })
            last_day = day
        margin_groups[-1]['pages'].append(p)
    # v4 : rail de marge aligne sur la timeline — les notes de marge sont
    # rattachees au jour correspondant des sections auto (kind='day').
    # margin_by_day : day 'YYYY-MM-DD' -> liste de pages de marge.
    # margin_rest   : groupes sans jour-section correspondant (affiches en bas).
    section_days = set()
    for s1 in pages.get('structured', []):
        sec = s1['section']
        if sec.get('kind') == 'day' and sec.get('date_start'):
            section_days.add(_norm_ts(sec['date_start'])[:10])
        for sub in s1.get('subsections', []):
            c2 = sub['section']
            if c2.get('kind') == 'day' and c2.get('date_start'):
                section_days.add(_norm_ts(c2['date_start'])[:10])
    margin_by_day = {}
    margin_rest = []
    for g in margin_groups:
        if g['day'] and g['day'] in section_days:
            margin_by_day.setdefault(g['day'], []).extend(g['pages'])
        else:
            margin_rest.append(g)
    # v4.4 : videos de l'espace jamais rattachees a un album (bug historique
    # d'upload) -> proposees a la recuperation en tete d'album
    orphan_videos = [dict(r) for r in query("""
        SELECT v.* FROM videos v
        WHERE v.couple_id = ?
          AND NOT EXISTS (SELECT 1 FROM album_pages ap WHERE ap.video_id = v.id)
        ORDER BY v.added_at ASC
    """, (c['couple_id'],))]
    # v5.12 : etapes retirees du voyage (restaurables) — « je n'ai pas fait
    # ce point » se defait comme tout le reste
    etapes_corbeille = [dict(r) for r in query("""
        SELECT id, title, pin_kind, deleted_at FROM carnet_items
        WHERE carnet_id=? AND kind='location' AND deleted_at IS NOT NULL
        ORDER BY deleted_at DESC
    """, (cid_carnet,))]
    # v5.8 : epingles sur photo — payload pour la lightbox + compteur tuile
    photo_ids = sorted({p['photo_id'] for p in pages['all'] if p.get('photo_id')})
    photo_notes = {}
    if photo_ids:
        ph_n = ','.join('?' * len(photo_ids))
        for r in query(f"""
            SELECT pn.id, pn.photo_id, pn.x, pn.y, pn.texte, pn.auteur_id,
                   pn.created_at, u.display_name AS auteur
            FROM photo_notes pn LEFT JOIN users u ON u.id = pn.auteur_id
            WHERE pn.photo_id IN ({ph_n})
            ORDER BY pn.created_at ASC, pn.id ASC
        """, tuple(photo_ids)):
            photo_notes.setdefault(r['photo_id'], []).append(dict(r))
    for p in pages['all']:
        p['photo_notes_n'] = len(photo_notes.get(p.get('photo_id'), []))
    # couleur d'epingle par membre (ordre d'arrivee dans l'espace)
    membres = query("""SELECT u.id, u.display_name FROM espace_members em
                       JOIN users u ON u.id = em.user_id
                       WHERE em.espace_id=? ORDER BY em.joined_at, u.id""",
                    (c['couple_id'],))
    pin_palette = ['#C4684F', '#4A6FA5', '#5B8A5A', '#8A6FA5', '#B08B3E']
    member_colors = {str(m['id']): pin_palette[i % len(pin_palette)]
                     for i, m in enumerate(membres)}
    # v5.7 : photo hero de chaque scene -> version 1024px garantie
    # (generation paresseuse pour les photos d'avant v5.7)
    for s1 in pages.get('structured', []):
        hero_lists = [sub['pages'] for sub in s1.get('subsections', [])]
        if s1.get('pages'):
            hero_lists.append(s1['pages'])
        for plist in hero_lists:
            first_photo = next((p for p in plist if p.get('photo_path')), None)
            if first_photo and not first_photo.get('photo_mid'):
                m = _ensure_mid(first_photo['photo_id'],
                                first_photo['photo_path'],
                                first_photo.get('photo_mid'))
                if m:
                    first_photo['photo_mid'] = m
    return render_template('album.html', carnet=c,
        etapes_by_day=etapes_by_day, etapes_all=etapes_all,
        pin_kinds=PIN_KINDS,
        orphan_videos=orphan_videos,
        main_pages=pages['main'], margin_pages=pages['margin'],
        margin_groups=margin_groups,
        margin_by_day=margin_by_day, margin_rest=margin_rest,
        structured=pages.get('structured', []),
        orphans=pages.get('orphans', []),
        hidden_pages=pages.get('hidden', []),
        geo_photos=geo_photos, geo_etapes=geo_etapes,
        photo_notes=photo_notes, member_colors=member_colors,
        etapes_corbeille=etapes_corbeille,
        types=CARNET_TYPES, sort_mode=sort_mode)


def _safe_float(v):
    try:
        f = float(v)
        if f != f:  # NaN
            return None
        return f
    except (TypeError, ValueError):
        return None


@app.route('/carnet/<int:cid_carnet>/photos', methods=['POST'])
@couple_required
def carnet_upload_photos(cid_carnet):
    """
    Upload multi-photos. Le client envoie en parallele des photos :
    - taken_at[]   : ISO date extraite cote client (compression Canvas
                     supprime les EXIF cote serveur)
    - gps_lat[]    : latitude EXIF si dispo
    - gps_lng[]    : longitude EXIF si dispo
    - is_margin[]  : '1' si la photo doit aller en note marginale, sinon '0'
    Tous indexes sur la meme position que les photos.
    """
    c = _get_carnet_or_404(cid_carnet)
    if not csrf_check():
        return jsonify({'ok': False, 'error': 'Session expiree (CSRF)'}), 403
    files = request.files.getlist('photos')
    client_taken = request.form.getlist('taken_at')
    client_lat = request.form.getlist('gps_lat')
    client_lng = request.form.getlist('gps_lng')
    client_margin = request.form.getlist('is_margin')
    log.info("upload carnet=%s : %d fichier(s) recu(s)", cid_carnet, len(files))
    if not files:
        return jsonify({'ok': False, 'error': 'Aucun fichier recu'}), 400
    created = []
    errors = []
    for idx, f in enumerate(files):
        if not f or not f.filename:
            errors.append(f"#{idx+1}: fichier vide")
            continue
        try:
            data = _save_uploaded_photo(f, c['couple_id'])
        except Exception as e:
            tb = traceback.format_exc()
            log.error("upload #%d (%s) ECHEC: %s\n%s", idx + 1, f.filename, e, tb)
            errors.append(f"{f.filename}: {type(e).__name__}: {e}")
            continue
        # Source de verite : EXIF cote serveur (data) > client > nom de
        # fichier > rien. Le client envoie ses lectures exifr ; le serveur
        # lit aussi via Pillow quand l'original arrive non compresse.
        # v5.7 : l'origine de la date est TRACEE (taken_at_source) et le
        # fallback nom de fichier se voit a l'ecran (regle R4).
        taken_source = 'exif' if data.get('taken_at') else ''
        ct = client_taken[idx] if idx < len(client_taken) else ''
        if ct and ct != 'null' and not data.get('taken_at'):
            data['taken_at'] = ct
            taken_source = 'exif'
        if not data.get('taken_at'):
            devine = _date_from_filename(f.filename)
            if devine:
                data['taken_at'] = devine
                taken_source = 'fichier'
                log.info("upload #%d (%s) : date devinee depuis le nom -> %s",
                         idx + 1, f.filename, devine)
        gps_lat = data.get('gps_lat')
        gps_lng = data.get('gps_lng')
        if gps_lat is None:
            gps_lat = _safe_float(client_lat[idx]) if idx < len(client_lat) else None
        if gps_lng is None:
            gps_lng = _safe_float(client_lng[idx]) if idx < len(client_lng) else None
        is_margin = (client_margin[idx] == '1') if idx < len(client_margin) else False
        # v1.2.4 — Reinjecte les EXIF dans le fichier final + thumbnail
        _inject_exif_to_jpeg(os.path.join(UPLOAD_DIR, data['file_path']),
                             data.get('taken_at'), gps_lat, gps_lng)
        _inject_exif_to_jpeg(os.path.join(UPLOAD_DIR, data['thumb_path']),
                             data.get('taken_at'), gps_lat, gps_lng)
        photo_id = execute(
            "INSERT INTO photos (couple_id, file_path, thumb_path, width, height, "
            "taken_at, gps_lat, gps_lng, added_by, taken_at_source, orig_filename, "
            "lieu_source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (c['couple_id'], data['file_path'], data['thumb_path'],
             data['width'], data['height'], data['taken_at'],
             gps_lat, gps_lng, session['uid'],
             taken_source, (f.filename or '')[:255],
             'exif' if gps_lat is not None else '')
        )
        if data.get('mid_path'):
            execute("UPDATE photos SET mid_path=? WHERE id=?",
                    (data['mid_path'], photo_id))
        _enrich_photo_geo(photo_id, gps_lat, gps_lng)
        pos = _next_page_position(cid_carnet)
        page_id = execute(
            "INSERT INTO album_pages (carnet_id, type, position, photo_id, "
            "is_margin, added_by) VALUES (?,?,?,?,?,?)",
            (cid_carnet, 'photo', pos, photo_id, 1 if is_margin else 0, session['uid'])
        )
        created.append({
            'page_id': page_id,
            'photo_id': photo_id,
            'thumb_url': url_for('serve_upload', filename=data['thumb_path']),
            'full_url': url_for('serve_upload', filename=data['file_path']),
            'taken_at': data['taken_at'],
            'gps_lat': gps_lat, 'gps_lng': gps_lng,
            'is_margin': is_margin,
            'width': data['width'], 'height': data['height'],
        })
    log.info("upload carnet=%s : %d cree(s), %d erreur(s)", cid_carnet, len(created), len(errors))
    # Brief 05 : recalcul des sections auto apres chaque ajout
    try:
        _recompute_sections(cid_carnet)
    except Exception as e:
        log.warning("recompute sections fail: %s", e)
    if created:
        _log_activity(c['couple_id'], session['uid'], 'photos_added',
                      target_carnet_id=cid_carnet,
                      payload={'count': len(created)})
    return jsonify({'ok': True, 'created': created, 'errors': errors})


@app.route('/album_page/<int:page_id>/attach_photo', methods=['POST'])
@couple_required
def page_attach_photo(page_id):
    """Attache une photo a une page existante (souvent un bloc texte) :
    le bloc devient mixte texte + photo, dans le meme cadre visuel."""
    if not csrf_check():
        return jsonify({'ok': False, 'error': 'CSRF'}), 403
    page = query("SELECT ap.*, c.couple_id FROM album_pages ap "
                 "JOIN carnets c ON c.id=ap.carnet_id WHERE ap.id=?",
                 (page_id,), one=True)
    if not page or page['couple_id'] != current_espace_id():
        return jsonify({'ok': False, 'error': '404'}), 404
    f = request.files.get('photo')
    if not f or not f.filename:
        return jsonify({'ok': False, 'error': 'Aucun fichier'}), 400
    try:
        data = _save_uploaded_photo(f, page['couple_id'])
    except Exception as e:
        log.error("attach_photo page=%s ECHEC: %s\n%s", page_id, e, traceback.format_exc())
        return jsonify({'ok': False, 'error': str(e)}), 500
    # EXIF override par client si dispo
    ct = request.form.get('taken_at') or ''
    if ct and ct != 'null':
        data['taken_at'] = ct
    gps_lat = _safe_float(request.form.get('gps_lat'))
    gps_lng = _safe_float(request.form.get('gps_lng'))
    # v1.2.4 — reinjection EXIF
    _inject_exif_to_jpeg(os.path.join(UPLOAD_DIR, data['file_path']),
                         data.get('taken_at'), gps_lat, gps_lng)
    _inject_exif_to_jpeg(os.path.join(UPLOAD_DIR, data['thumb_path']),
                         data.get('taken_at'), gps_lat, gps_lng)
    photo_id = execute(
        "INSERT INTO photos (couple_id, file_path, thumb_path, width, height, "
        "taken_at, gps_lat, gps_lng, added_by) VALUES (?,?,?,?,?,?,?,?,?)",
        (page['couple_id'], data['file_path'], data['thumb_path'],
         data['width'], data['height'], data['taken_at'],
         gps_lat, gps_lng, session['uid'])
    )
    _enrich_photo_geo(photo_id, gps_lat, gps_lng)
    execute(
        "UPDATE album_pages SET photo_id=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (photo_id, page_id)
    )
    return jsonify({
        'ok': True, 'photo_id': photo_id,
        'thumb_url': url_for('serve_upload', filename=data['thumb_path']),
        'full_url': url_for('serve_upload', filename=data['file_path']),
        'taken_at': data['taken_at'],
        'gps_lat': gps_lat, 'gps_lng': gps_lng,
    })


@app.route('/album_page/<int:page_id>/detach_photo', methods=['POST'])
@couple_required
def page_detach_photo(page_id):
    """Retire la photo d'un bloc mixte (le texte reste)."""
    if not csrf_check():
        return jsonify({'ok': False, 'error': 'CSRF'}), 403
    page = query("SELECT ap.*, c.couple_id FROM album_pages ap "
                 "JOIN carnets c ON c.id=ap.carnet_id WHERE ap.id=?",
                 (page_id,), one=True)
    if not page or page['couple_id'] != current_espace_id():
        return jsonify({'ok': False, 'error': '404'}), 404
    execute(
        "UPDATE album_pages SET photo_id=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (page_id,)
    )
    return jsonify({'ok': True})


@app.route('/carnet/<int:cid_carnet>/pages/reorder', methods=['POST'])
@couple_required
def carnet_reorder_pages(cid_carnet):
    """Drag & drop : nouvel ordre des pages. Bascule en sort_mode='manual'."""
    c = _get_carnet_or_404(cid_carnet)
    if not csrf_check():
        return jsonify({'ok': False, 'error': 'CSRF'}), 403
    raw = request.form.getlist('page_id')
    try:
        ids = [int(x) for x in raw if str(x).isdigit()]
    except ValueError:
        return jsonify({'ok': False, 'error': 'IDs invalides'}), 400
    if not ids:
        return jsonify({'ok': False, 'error': 'Aucun id'}), 400
    placeholders = ','.join('?' * len(ids))
    valid = query(
        f"SELECT id FROM album_pages WHERE carnet_id=? AND id IN ({placeholders})",
        tuple([cid_carnet] + ids)
    )
    valid_set = {r['id'] for r in valid}
    if set(ids) - valid_set:
        return jsonify({'ok': False, 'error': 'Pages externes'}), 400
    conn = get_db()
    try:
        for pos, pid in enumerate(ids):
            conn.execute("UPDATE album_pages SET position=? WHERE id=?", (pos, pid))
        # v5.18 : plus de bascule sort_mode='manual' — deplacer=re-dater
        # (v5.13) a rendu l'ordre manuel obsolete ; il inversait les jours
        # du livre et annulait les re-datations (audit livre §3).
        conn.commit()
    finally:
        conn.close()
    return jsonify({'ok': True, 'mode': 'manual'})


@app.route('/album_page/<int:page_id>/deplacer', methods=['POST'])
@couple_required
def page_deplacer(page_id):
    """v5.13 — Deplacer une page, c'est la RE-DATER (retour telephone du
    2026-08-14 : le drag posait une position a plat que les sections par
    date ecrasaient au rendu suivant — la photo « revenait » a sa date
    d'import). Ici la chronologie reste l'unique verite :
    - glissee entre deux voisines datees -> point median de leurs dates ;
    - en tete/queue de liste -> voisine -1 min / +1 min ;
    - dans un jour vide (data-day) -> ce jour a 12:00.
    S'applique aux pages photo et video ; un bloc texte deplace son ancre
    (created_at, qui EST sa cle chronologique). Source tracee 'manuel'."""
    if not csrf_check():
        return jsonify({'ok': False, 'error': 'CSRF'}), 403
    page = query("SELECT ap.*, c.couple_id FROM album_pages ap "
                 "JOIN carnets c ON c.id=ap.carnet_id WHERE ap.id=?",
                 (page_id,), one=True)
    if not page or page['couple_id'] != current_espace_id():
        return jsonify({'ok': False, 'error': '404'}), 404

    def _page_ts(pid):
        if not pid or not str(pid).isdigit():
            return None
        r = query("""SELECT COALESCE(p.taken_at, v.taken_at, ap.created_at) AS ts
                     FROM album_pages ap
                     LEFT JOIN photos p ON p.id = ap.photo_id
                     LEFT JOIN videos v ON v.id = ap.video_id
                     WHERE ap.id=? AND ap.carnet_id=?""",
                  (int(pid), page['carnet_id']), one=True)
        return _norm_ts(r['ts']) if r and r['ts'] else None

    prev_ts = _page_ts(request.form.get('prev_id'))
    next_ts = _page_ts(request.form.get('next_id'))
    day = (request.form.get('day') or '').strip()[:10]

    def _parse(ts):
        try:
            return datetime.fromisoformat(ts[:19].replace(' ', 'T'))
        except (ValueError, TypeError):
            return None
    d_prev, d_next = _parse(prev_ts), _parse(next_ts)
    if d_prev and d_next:
        nouveau = d_prev + (d_next - d_prev) / 2
    elif d_prev:
        nouveau = d_prev + timedelta(minutes=1)
    elif d_next:
        nouveau = d_next - timedelta(minutes=1)
    elif day:
        try:
            nouveau = datetime.strptime(day, '%Y-%m-%d').replace(hour=12)
        except ValueError:
            return jsonify({'ok': False, 'error': 'bad_day'}), 400
    else:
        return jsonify({'ok': False, 'error': 'no_target'}), 400
    iso = nouveau.isoformat(timespec='seconds')

    if page['photo_id']:
        execute("UPDATE photos SET taken_at=?, taken_at_source='manuel' WHERE id=?",
                (iso, page['photo_id']))
    elif page['video_id']:
        execute("UPDATE videos SET taken_at=? WHERE id=?", (iso, page['video_id']))
    else:
        # bloc texte : created_at est sa cle chronologique
        execute("UPDATE album_pages SET created_at=? WHERE id=?", (iso, page_id))
    execute("UPDATE album_pages SET manual_order=0 WHERE id=?", (page_id,))
    try:
        _recompute_sections(page['carnet_id'])
    except Exception as e:
        log.warning("recompute apres deplacement: %s", e)
    return jsonify({'ok': True, 'taken_at': iso})


@app.route('/carnet/<int:cid_carnet>/pages/sort_chrono', methods=['POST'])
@couple_required
def carnet_sort_chrono(cid_carnet):
    """Reset au tri chronologique (oublie l'ordre manuel)."""
    c = _get_carnet_or_404(cid_carnet)
    if not csrf_check():
        return jsonify({'ok': False, 'error': 'CSRF'}), 403
    execute("UPDATE carnets SET sort_mode='chrono' WHERE id=?", (cid_carnet,))
    return jsonify({'ok': True, 'mode': 'chrono'})


@app.route('/carnet/<int:cid_carnet>/items/reorder', methods=['POST'])
@couple_required
def carnet_reorder_items(cid_carnet):
    """Drag & drop des items d'un carnet de souhait."""
    c = _get_carnet_or_404(cid_carnet)
    if not csrf_check():
        return jsonify({'ok': False, 'error': 'CSRF'}), 403
    raw = request.form.getlist('item_id')
    try:
        ids = [int(x) for x in raw if str(x).isdigit()]
    except ValueError:
        return jsonify({'ok': False, 'error': 'IDs invalides'}), 400
    if not ids:
        return jsonify({'ok': False, 'error': 'Aucun id'}), 400
    placeholders = ','.join('?' * len(ids))
    valid = query(
        f"SELECT id FROM carnet_items WHERE carnet_id=? AND id IN ({placeholders})",
        tuple([cid_carnet] + ids)
    )
    valid_set = {r['id'] for r in valid}
    if set(ids) - valid_set:
        return jsonify({'ok': False, 'error': 'Items externes'}), 400
    conn = get_db()
    try:
        for pos, iid in enumerate(ids):
            conn.execute("UPDATE carnet_items SET position=? WHERE id=?", (pos, iid))
        conn.commit()
    finally:
        conn.close()
    return jsonify({'ok': True})


@app.route('/album_page/<int:page_id>/margin', methods=['POST'])
@couple_required
def page_toggle_margin(page_id):
    """Bascule une page entre album principal et note marginale."""
    if not csrf_check():
        return jsonify({'ok': False, 'error': 'CSRF'}), 403
    page = query("SELECT ap.*, c.couple_id FROM album_pages ap "
                 "JOIN carnets c ON c.id=ap.carnet_id WHERE ap.id=?",
                 (page_id,), one=True)
    if not page or page['couple_id'] != current_espace_id():
        return jsonify({'ok': False, 'error': '404'}), 404
    new_val = 0 if page['is_margin'] else 1
    execute(
        "UPDATE album_pages SET is_margin=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (new_val, page_id)
    )
    return jsonify({'ok': True, 'is_margin': bool(new_val)})


@app.route('/album_page/<int:page_id>/caption', methods=['POST'])
@couple_required
def page_update_caption(page_id):
    if not csrf_check():
        return jsonify({'ok': False, 'error': 'CSRF'}), 403
    page = query("SELECT ap.*, c.couple_id FROM album_pages ap "
                 "JOIN carnets c ON c.id=ap.carnet_id WHERE ap.id=?",
                 (page_id,), one=True)
    if not page or page['couple_id'] != current_espace_id():
        return jsonify({'ok': False, 'error': '404'}), 404
    caption = (request.form.get('caption') or '').strip()
    execute(
        "UPDATE album_pages SET caption=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (caption, page_id)
    )
    if caption:
        preview = caption if len(caption) <= 80 else caption[:77] + '…'
        _log_activity(page['couple_id'], session['uid'], 'caption_updated',
                      target_carnet_id=page['carnet_id'],
                      payload={'preview': preview})
    return jsonify({'ok': True, 'caption': caption})


@app.route('/album_page/<int:page_id>/text', methods=['POST'])
@couple_required
def page_update_text(page_id):
    if not csrf_check():
        return jsonify({'ok': False, 'error': 'CSRF'}), 403
    page = query("SELECT ap.*, c.couple_id FROM album_pages ap "
                 "JOIN carnets c ON c.id=ap.carnet_id WHERE ap.id=?",
                 (page_id,), one=True)
    if not page or page['couple_id'] != current_espace_id():
        return jsonify({'ok': False, 'error': '404'}), 404
    text = (request.form.get('text_content') or '').strip()
    execute(
        "UPDATE album_pages SET text_content=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (text, page_id)
    )
    return jsonify({'ok': True, 'text_content': text})


@app.route('/album_page/<int:page_id>/taken_at', methods=['POST'])
@couple_required
def page_update_taken_at(page_id):
    """Saisie manuelle de la date sur une photo sans EXIF (orphelin)."""
    if not csrf_check():
        return jsonify({'ok': False, 'error': 'CSRF'}), 403
    page = query("SELECT ap.*, c.couple_id FROM album_pages ap "
                 "JOIN carnets c ON c.id=ap.carnet_id WHERE ap.id=?",
                 (page_id,), one=True)
    if not page or page['couple_id'] != current_espace_id():
        return jsonify({'ok': False, 'error': '404'}), 404
    if not page['photo_id']:
        return jsonify({'ok': False, 'error': 'no_photo'}), 400
    raw = (request.form.get('taken_at') or '').strip()
    # datetime-local renvoie "YYYY-MM-DDTHH:MM" — on normalise vers ISO complet.
    iso = None
    if raw:
        try:
            iso = datetime.strptime(raw, '%Y-%m-%dT%H:%M').isoformat()
        except ValueError:
            try:
                iso = datetime.strptime(raw, '%Y-%m-%dT%H:%M:%S').isoformat()
            except ValueError:
                return jsonify({'ok': False, 'error': 'bad_date'}), 400
    # v5.7 : la date saisie a la main est tracee comme telle (badge a l'ecran)
    execute("UPDATE photos SET taken_at=?, taken_at_source='manuel' WHERE id=?",
            (iso, page['photo_id']))
    execute("UPDATE album_pages SET updated_at=CURRENT_TIMESTAMP WHERE id=?", (page_id,))
    # v5.7 : la photo datee rejoint sa journee sans attendre un recompute manuel
    try:
        _recompute_sections(page['carnet_id'])
    except Exception as e:
        log.warning("recompute apres datation manuelle: %s", e)
    return jsonify({'ok': True, 'taken_at': iso})


@app.route('/carnet/<int:cid_carnet>/photos/dater_lot', methods=['POST'])
@couple_required
def carnet_dater_lot(cid_carnet):
    """v5.7 — Datation PAR LOT des photos sans date (audit P1 : 26 % des
    photos etaient sans date, et la saisie n'existait que photo par photo).
    Recoit page_ids[] + taken_at (datetime-local). La 1re photo du lot prend
    la date exacte ; les suivantes s'espacent d'une minute pour conserver
    l'ordre de selection a l'interieur de la journee. Source tracee 'manuel'."""
    c = _get_carnet_or_404(cid_carnet)
    if not csrf_check():
        return jsonify({'ok': False, 'error': 'CSRF'}), 403
    raw = (request.form.get('taken_at') or '').strip()
    try:
        base = datetime.strptime(raw, '%Y-%m-%dT%H:%M')
    except ValueError:
        try:
            base = datetime.strptime(raw, '%Y-%m-%dT%H:%M:%S')
        except ValueError:
            return jsonify({'ok': False, 'error': 'bad_date'}), 400
    ids = [i for i in request.form.getlist('page_ids') if str(i).isdigit()]
    if not ids:
        return jsonify({'ok': False, 'error': 'no_pages'}), 400
    # Cloisonnement : seules les pages de CE carnet (donc de cet espace,
    # deja verifie par _get_carnet_or_404) sont touchees.
    ph = ','.join('?' * len(ids))
    pages = query(f"""
        SELECT ap.id, ap.photo_id FROM album_pages ap
        WHERE ap.carnet_id=? AND ap.id IN ({ph}) AND ap.photo_id IS NOT NULL
    """, tuple([cid_carnet] + ids))
    by_id = {str(p['id']): p['photo_id'] for p in pages}
    dates = 0
    for k, pid_str in enumerate(ids):
        photo_id = by_id.get(str(pid_str))
        if not photo_id:
            continue  # page d'un autre carnet ou sans photo : ignoree
        iso = (base + timedelta(minutes=k)).isoformat(timespec='seconds')
        execute("UPDATE photos SET taken_at=?, taken_at_source='manuel' WHERE id=?",
                (iso, photo_id))
        dates += 1
    if dates:
        try:
            _recompute_sections(cid_carnet)
        except Exception as e:
            log.warning("recompute apres datation par lot: %s", e)
        _log_activity(c['couple_id'], session['uid'], 'photos_dated',
                      target_carnet_id=cid_carnet, payload={'count': dates})
    return jsonify({'ok': True, 'dated': dates, 'ignored': len(ids) - dates})


def _photo_of_espace_or_none(photo_id):
    """v5.8 — La photo si (et seulement si) elle appartient a l'espace
    courant. Un SELECT par id seul serait un defaut de cloisonnement (R1)."""
    return query("SELECT * FROM photos WHERE id=? AND couple_id=?",
                 (photo_id, current_espace_id()), one=True)


@app.route('/photo/<int:photo_id>/notes', methods=['POST'])
@couple_required
def photo_note_add(photo_id):
    """v5.8 — Ajoute une epingle (x, y, texte) sur une photo de l'espace.
    x et y sont NORMALISES [0,1] : valides cote serveur, pas seulement au clic."""
    if not csrf_check():
        return jsonify({'ok': False, 'error': 'CSRF'}), 403
    photo = _photo_of_espace_or_none(photo_id)
    if not photo:
        return jsonify({'ok': False, 'error': '404'}), 404
    texte = (request.form.get('texte') or '').strip()
    if not texte:
        return jsonify({'ok': False, 'error': 'texte_vide'}), 400
    x = _safe_float(request.form.get('x'))
    y = _safe_float(request.form.get('y'))
    if x is None or y is None or not (0.0 <= x <= 1.0) or not (0.0 <= y <= 1.0):
        return jsonify({'ok': False, 'error': 'coords'}), 400
    note_id = execute(
        "INSERT INTO photo_notes (photo_id, x, y, texte, auteur_id) VALUES (?,?,?,?,?)",
        (photo_id, round(x, 4), round(y, 4), texte[:1000], session['uid'])
    )
    row = query("""SELECT pn.*, u.display_name AS auteur FROM photo_notes pn
                   LEFT JOIN users u ON u.id = pn.auteur_id WHERE pn.id=?""",
                (note_id,), one=True)
    return jsonify({'ok': True, 'note': dict(row)})


@app.route('/photo/<int:photo_id>/lieu', methods=['POST'])
@couple_required
def photo_set_lieu(photo_id):
    """v5.10 — Pose (ou corrige) le LIEU d'une photo sans GPS : l'utilisateur
    choisit un resultat de /geo/search (lat, lng, label). Le reverse-geocoding
    enrichit pays/ville en arriere-plan ; le label choisi reste prioritaire."""
    if not csrf_check():
        return jsonify({'ok': False, 'error': 'CSRF'}), 403
    photo = _photo_of_espace_or_none(photo_id)
    if not photo:
        return jsonify({'ok': False, 'error': '404'}), 404
    lat = _safe_float(request.form.get('lat'))
    lng = _safe_float(request.form.get('lng'))
    label = (request.form.get('label') or '').strip()[:200]
    if lat is None or lng is None or not (-90 <= lat <= 90) or not (-180 <= lng <= 180):
        return jsonify({'ok': False, 'error': 'coords'}), 400
    execute("UPDATE photos SET gps_lat=?, gps_lng=?, lieu_source='manuel' WHERE id=?",
            (lat, lng, photo_id))
    _enrich_photo_geo(photo_id, lat, lng)   # best effort (cache Nominatim)
    if label:
        execute("UPDATE photos SET address_full=? WHERE id=?", (label, photo_id))
    # les sections jour/lieu de tous les carnets qui montrent cette photo
    # se recalculent (une photo peut vivre dans plusieurs albums)
    for r in query("SELECT DISTINCT carnet_id FROM album_pages WHERE photo_id=?", (photo_id,)):
        try:
            _recompute_sections(r['carnet_id'])
        except Exception as e:
            log.warning("recompute apres lieu manuel: %s", e)
    return jsonify({'ok': True, 'lat': lat, 'lng': lng, 'label': label})


@app.route('/photo_note/<int:note_id>/supprimer', methods=['POST'])
@couple_required
def photo_note_supprimer(note_id):
    """v5.8 — Retire une epingle. Seul son AUTEUR la retire (le contenu
    des autres ne disparait pas sous vos doigts, D4)."""
    if not csrf_check():
        return jsonify({'ok': False, 'error': 'CSRF'}), 403
    note = query("""SELECT pn.* FROM photo_notes pn
                    JOIN photos p ON p.id = pn.photo_id
                    WHERE pn.id=? AND p.couple_id=?""",
                 (note_id, current_espace_id()), one=True)
    if not note:
        return jsonify({'ok': False, 'error': '404'}), 404
    if note['auteur_id'] != session['uid']:
        return jsonify({'ok': False, 'error': 'pas_auteur'}), 403
    execute("DELETE FROM photo_notes WHERE id=?", (note_id,))
    return jsonify({'ok': True})


@app.route('/carnet/<int:cid_carnet>/margin_note', methods=['POST'])
@couple_required
def carnet_add_margin_note(cid_carnet):
    """v3 — Ajoute une note de marge (texte + photo optionnelle) en 1 POST."""
    c = _get_carnet_or_404(cid_carnet)
    if not csrf_check():
        return jsonify({'ok': False, 'error': 'CSRF'}), 403
    text = (request.form.get('text') or '').strip()
    caption = (request.form.get('caption') or '').strip()
    f = request.files.get('photo')
    if not text and not caption and not (f and f.filename):
        return jsonify({'ok': False, 'error': 'Note vide'}), 400
    photo_id = None
    if f and f.filename:
        try:
            data = _save_uploaded_photo(f, c['couple_id'])
            gps_lat = _safe_float(request.form.get('gps_lat'))
            gps_lng = _safe_float(request.form.get('gps_lng'))
            taken_at = request.form.get('taken_at') or data.get('taken_at')
            _inject_exif_to_jpeg(os.path.join(UPLOAD_DIR, data['file_path']),
                                 taken_at, gps_lat, gps_lng)
            _inject_exif_to_jpeg(os.path.join(UPLOAD_DIR, data['thumb_path']),
                                 taken_at, gps_lat, gps_lng)
            photo_id = execute(
                "INSERT INTO photos (couple_id, file_path, thumb_path, width, height, "
                "taken_at, gps_lat, gps_lng, added_by) VALUES (?,?,?,?,?,?,?,?,?)",
                (c['couple_id'], data['file_path'], data['thumb_path'],
                 data['width'], data['height'], taken_at, gps_lat, gps_lng, session['uid'])
            )
            _enrich_photo_geo(photo_id, gps_lat, gps_lng)
        except Exception as e:
            log.error("margin_note photo upload: %s", e)
            return jsonify({'ok': False, 'error': 'Photo : ' + str(e)}), 500
    pos = _next_page_position(cid_carnet)
    page_type = 'photo' if photo_id else 'text'
    # v4.2 : une note texte sans corps mais avec legende -> la legende EST le texte
    if page_type == 'text' and not text and caption:
        text, caption = caption, ''
    # v4.2 : ancre vers une page photo · v4.6 : OU vers une etape du planning
    anchor_id = None
    raw_anchor = request.form.get('anchor_page_id')
    if raw_anchor and str(raw_anchor).isdigit():
        a = query("SELECT id FROM album_pages WHERE id=? AND carnet_id=? "
                  "AND COALESCE(is_margin,0)=0", (int(raw_anchor), cid_carnet), one=True)
        if a:
            anchor_id = a['id']
    anchor_item = None
    raw_item = request.form.get('anchor_item_id')
    if raw_item and str(raw_item).isdigit():
        it = query("SELECT id FROM carnet_items WHERE id=? AND carnet_id=?",
                   (int(raw_item), cid_carnet), one=True)
        if it:
            anchor_item = it['id']
    page_id = execute(
        "INSERT INTO album_pages (carnet_id, type, position, photo_id, "
        "text_content, caption, is_margin, anchor_page_id, anchor_item_id, added_by) "
        "VALUES (?,?,?,?,?,?,1,?,?,?)",
        (cid_carnet, page_type, pos, photo_id, text, caption, anchor_id,
         anchor_item, session['uid'])
    )
    cap_short = caption or text or ''
    if len(cap_short) > 80:
        cap_short = cap_short[:77] + '…'
    _log_activity(c['couple_id'], session['uid'], 'margin_note_added',
                  target_carnet_id=cid_carnet,
                  payload={'caption': cap_short, 'has_photo': bool(photo_id)})
    return jsonify({'ok': True, 'page_id': page_id, 'position': pos})


@app.route('/carnet/<int:cid_carnet>/text', methods=['POST'])
@couple_required
def carnet_add_text(cid_carnet):
    c = _get_carnet_or_404(cid_carnet)
    if not csrf_check():
        return jsonify({'ok': False, 'error': 'CSRF'}), 403
    is_margin = request.form.get('is_margin') == '1'
    pos = _next_page_position(cid_carnet)
    page_id = execute(
        "INSERT INTO album_pages (carnet_id, type, position, text_content, "
        "is_margin, added_by) VALUES (?,?,?,?,?,?)",
        (cid_carnet, 'text', pos, '', 1 if is_margin else 0, session['uid'])
    )
    return jsonify({'ok': True, 'page_id': page_id, 'position': pos, 'is_margin': is_margin})


@app.route('/album_page/<int:page_id>/anchor', methods=['POST'])
@couple_required
def page_set_anchor(page_id):
    """v4.2 : relie (ou detache) une note de marge a une page photo de l'album."""
    if not csrf_check():
        return jsonify({'ok': False, 'error': 'CSRF'}), 403
    note = query("SELECT ap.*, c.couple_id AS cpl FROM album_pages ap "
                 "JOIN carnets c ON c.id=ap.carnet_id WHERE ap.id=?",
                 (page_id,), one=True)
    if not note or note['cpl'] != current_espace_id():
        return jsonify({'ok': False, 'error': '404'}), 404
    if not note['is_margin']:
        return jsonify({'ok': False, 'error': 'Seule une note de marge peut etre reliee'}), 400
    raw_item = (request.form.get('anchor_item_id') or '').strip()
    if raw_item:
        if not raw_item.isdigit():
            return jsonify({'ok': False, 'error': 'Ancre invalide'}), 400
        t = query("SELECT id FROM carnet_items WHERE id=? AND carnet_id=?",
                  (int(raw_item), note['carnet_id']), one=True)
        if not t:
            return jsonify({'ok': False, 'error': 'Etape introuvable'}), 404
        execute("UPDATE album_pages SET anchor_item_id=?, anchor_page_id=NULL WHERE id=?",
                (t['id'], page_id))
        return jsonify({'ok': True, 'anchor_item_id': t['id']})
    raw = (request.form.get('anchor_page_id') or '').strip()
    if not raw:
        execute("UPDATE album_pages SET anchor_page_id=NULL, anchor_item_id=NULL WHERE id=?", (page_id,))
        return jsonify({'ok': True, 'anchor_page_id': None})
    if not raw.isdigit():
        return jsonify({'ok': False, 'error': 'Ancre invalide'}), 400
    target = query("SELECT id FROM album_pages WHERE id=? AND carnet_id=? "
                   "AND COALESCE(is_margin,0)=0", (int(raw), note['carnet_id']), one=True)
    if not target:
        return jsonify({'ok': False, 'error': 'Photo introuvable dans cet album'}), 404
    execute("UPDATE album_pages SET anchor_page_id=?, anchor_item_id=NULL WHERE id=?", (target['id'], page_id))
    return jsonify({'ok': True, 'anchor_page_id': target['id']})


@app.route('/album_page/<int:page_id>/supprimer', methods=['POST'])
@couple_required
def page_supprimer(page_id):
    if not csrf_check():
        return jsonify({'ok': False, 'error': 'CSRF'}), 403
    page = query("SELECT ap.*, c.couple_id FROM album_pages ap "
                 "JOIN carnets c ON c.id=ap.carnet_id WHERE ap.id=?",
                 (page_id,), one=True)
    if not page or page['couple_id'] != current_espace_id():
        return jsonify({'ok': False, 'error': '404'}), 404
    # v5.11 : la suppression se DEFAIT — la page passe en is_hidden=1
    # (corbeille de l'album) au lieu d'un DELETE. Rien ne disparait (D4).
    execute("UPDATE album_pages SET is_hidden=1 WHERE id=?", (page_id,))
    try:
        _recompute_sections(page['carnet_id'])
    except Exception as e:
        log.warning("recompute apres mise en corbeille: %s", e)
    return jsonify({'ok': True})


@app.route('/album_page/<int:page_id>/restaurer', methods=['POST'])
@couple_required
def page_restaurer(page_id):
    """v5.11 — Sort une page de la corbeille de l'album (is_hidden=0)."""
    if not csrf_check():
        return jsonify({'ok': False, 'error': 'CSRF'}), 403
    page = query("SELECT ap.*, c.couple_id FROM album_pages ap "
                 "JOIN carnets c ON c.id=ap.carnet_id WHERE ap.id=?",
                 (page_id,), one=True)
    if not page or page['couple_id'] != current_espace_id():
        return jsonify({'ok': False, 'error': '404'}), 404
    execute("UPDATE album_pages SET is_hidden=0 WHERE id=?", (page_id,))
    try:
        _recompute_sections(page['carnet_id'])
    except Exception as e:
        log.warning("recompute apres restauration: %s", e)
    return jsonify({'ok': True})


@app.route('/uploads/<path:filename>')
@couple_required
def serve_upload(filename):
    """Sert un fichier upload — verifie que le user appartient au couple proprietaire."""
    # Le path commence par <couple_id>/...
    parts = filename.split('/', 1)
    if len(parts) != 2:
        abort(404)
    try:
        owner_couple = int(parts[0])
    except ValueError:
        abort(404)
    if owner_couple != current_espace_id():
        abort(403)
    return send_from_directory(UPLOAD_DIR, filename, max_age=31536000)


# ══════════════════════════════════════════════════════════════════════
#                         v1.4.1 — VIDEOS
# ══════════════════════════════════════════════════════════════════════

def _save_uploaded_video(video_file, poster_file, couple_id):
    """Sauvegarde la video et son poster (extrait cote client). Retourne dict."""
    couple_dir = os.path.join(UPLOAD_DIR, str(couple_id))
    os.makedirs(couple_dir, exist_ok=True)
    token = secrets.token_urlsafe(12)

    # Extension video : on garde celle d'origine (mp4/mov/webm)
    ext = os.path.splitext(video_file.filename or 'v.mp4')[1].lower() or '.mp4'
    if ext not in ('.mp4', '.mov', '.webm', '.m4v'):
        ext = '.mp4'
    vname = f"{token}{ext}"
    vpath = os.path.join(couple_dir, vname)
    video_file.save(vpath)

    # Poster JPEG envoye par le client (deja compressed Canvas)
    pname = f"{token}_poster.jpg"
    ppath = os.path.join(couple_dir, pname)
    if poster_file and poster_file.filename:
        # Re-compresser via Pillow pour garantir JPEG propre
        try:
            img = Image.open(poster_file.stream)
            if img.mode != 'RGB':
                img = img.convert('RGB')
            img.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
            img.save(ppath, 'JPEG', quality=82, optimize=True)
        except Exception as e:
            log.warning("poster save echec, fallback raw save: %s", e)
            poster_file.stream.seek(0)
            poster_file.save(ppath)
    else:
        # Pas de poster : creer un placeholder gris
        img = Image.new('RGB', (1280, 720), (200, 195, 185))
        img.save(ppath, 'JPEG', quality=70)

    return {
        'file_path':   f"{couple_id}/{vname}",
        'poster_path': f"{couple_id}/{pname}",
        'token': token,
    }


@app.route('/carnet/<int:cid_carnet>/videos', methods=['POST'])
@couple_required
def carnet_upload_video(cid_carnet):
    """Upload d'une video + son poster (extrait cote client)."""
    c = _get_carnet_or_404(cid_carnet)
    if not csrf_check():
        return jsonify({'ok': False, 'error': 'CSRF'}), 403
    video = request.files.get('video')
    poster = request.files.get('poster')
    if not video or not video.filename:
        return jsonify({'ok': False, 'error': 'Aucune video'}), 400
    duration_s = _safe_float(request.form.get('duration_s'))
    width = request.form.get('width')
    height = request.form.get('height')
    is_margin = request.form.get('is_margin') == '1'
    try:
        width = int(width) if width else None
        height = int(height) if height else None
    except ValueError:
        width = height = None

    try:
        data = _save_uploaded_video(video, poster, c['couple_id'])
    except Exception as e:
        log.error("upload video echec: %s\n%s", e, traceback.format_exc())
        return jsonify({'ok': False, 'error': 'Save: ' + str(e)}), 500

    vid = execute(
        "INSERT INTO videos (couple_id, file_path, poster_path, duration_s, "
        "width, height, scan_token, added_by) VALUES (?,?,?,?,?,?,?,?)",
        (c['couple_id'], data['file_path'], data['poster_path'],
         duration_s, width, height, data['token'], session['uid'])
    )
    pos = _next_page_position(cid_carnet)
    page_id = execute(
        "INSERT INTO album_pages (carnet_id, type, position, video_id, "
        "is_margin, added_by) VALUES (?,?,?,?,?,?)",
        (cid_carnet, 'video', pos, vid, 1 if is_margin else 0, session['uid'])
    )
    try:
        _recompute_sections(cid_carnet)
    except Exception as e:
        log.warning("recompute sections fail: %s", e)
    return jsonify({
        'ok': True,
        'page_id': page_id, 'video_id': vid,
        'video_url': url_for('serve_upload', filename=data['file_path']),
        'poster_url': url_for('serve_upload', filename=data['poster_path']),
        'scan_token': data['token'],
        'public_url': url_for('video_public', token=data['token'], _external=True),
        'duration_s': duration_s,
        'is_margin': is_margin,
    })


@app.route('/carnet/<int:cid_carnet>/videos/init', methods=['POST'])
@couple_required
def carnet_video_init(cid_carnet):
    """Initialise un upload chunked. Retourne upload_id pour les chunks suivants."""
    c = _get_carnet_or_404(cid_carnet)
    if not csrf_check():
        return jsonify({'ok': False, 'error': 'CSRF'}), 403
    filename = (request.form.get('filename') or 'video.mp4').strip()
    try:
        total_size = int(request.form.get('total_size') or 0)
    except ValueError:
        total_size = 0
    if total_size <= 0 or total_size > 4 * 1024 * 1024 * 1024:  # max 4 Go
        return jsonify({'ok': False, 'error': 'Taille invalide (max 4 Go)'}), 400
    upload_id = secrets.token_urlsafe(12)
    couple_dir = os.path.join(UPLOAD_DIR, str(c['couple_id']), '_chunks')
    os.makedirs(couple_dir, exist_ok=True)
    ext = os.path.splitext(filename)[1].lower() or '.mp4'
    if ext not in ('.mp4', '.mov', '.webm', '.m4v', '.avi', '.mkv'):
        ext = '.mp4'
    tmp_path = os.path.join(couple_dir, f"{upload_id}{ext}")
    # Cree fichier vide
    open(tmp_path, 'wb').close()
    return jsonify({
        'ok': True,
        'upload_id': upload_id,
        'chunk_size': 4 * 1024 * 1024,  # suggestion : 4 Mo par chunk
        'total_size': total_size,
    })


@app.route('/carnet/<int:cid_carnet>/videos/chunk', methods=['POST'])
@couple_required
def carnet_video_chunk(cid_carnet):
    """Append un chunk a l'upload en cours."""
    c = _get_carnet_or_404(cid_carnet)
    if not csrf_check():
        return jsonify({'ok': False, 'error': 'CSRF'}), 403
    upload_id = request.form.get('upload_id') or ''
    if not upload_id or not all(ch.isalnum() or ch in '-_' for ch in upload_id):
        return jsonify({'ok': False, 'error': 'upload_id invalide'}), 400
    couple_dir = os.path.join(UPLOAD_DIR, str(c['couple_id']), '_chunks')
    # Cherche le fichier (extension peut varier)
    candidates = [f for f in os.listdir(couple_dir) if f.startswith(upload_id)]
    if not candidates:
        return jsonify({'ok': False, 'error': 'upload_id introuvable'}), 404
    tmp_path = os.path.join(couple_dir, candidates[0])
    chunk = request.files.get('chunk')
    if not chunk:
        return jsonify({'ok': False, 'error': 'Aucun chunk'}), 400
    # Append au fichier
    with open(tmp_path, 'ab') as f:
        chunk_data = chunk.stream.read()
        f.write(chunk_data)
    return jsonify({'ok': True, 'received': len(chunk_data),
                    'total_received': os.path.getsize(tmp_path)})


@app.route('/carnet/<int:cid_carnet>/videos/finalize', methods=['POST'])
@couple_required
def carnet_video_finalize(cid_carnet):
    """Termine l'upload : extrait poster, save BDD, crée la page album."""
    c = _get_carnet_or_404(cid_carnet)
    if not csrf_check():
        return jsonify({'ok': False, 'error': 'CSRF'}), 403
    upload_id = request.form.get('upload_id') or ''
    couple_dir = os.path.join(UPLOAD_DIR, str(c['couple_id']), '_chunks')
    candidates = [f for f in os.listdir(couple_dir) if f.startswith(upload_id)]
    if not candidates:
        return jsonify({'ok': False, 'error': 'upload_id introuvable'}), 404
    tmp_path = os.path.join(couple_dir, candidates[0])
    ext = os.path.splitext(candidates[0])[1] or '.mp4'

    # Deplace dans le dossier final avec son token public
    final_dir = os.path.join(UPLOAD_DIR, str(c['couple_id']))
    os.makedirs(final_dir, exist_ok=True)
    token = secrets.token_urlsafe(12)
    final_name = f"{token}{ext}"
    final_path = os.path.join(final_dir, final_name)
    shutil.move(tmp_path, final_path)

    # Poster : envoye en parallele par le client (ou placeholder)
    poster_file = request.files.get('poster')
    poster_name = f"{token}_poster.jpg"
    poster_path = os.path.join(final_dir, poster_name)
    try:
        if poster_file and poster_file.filename:
            img = Image.open(poster_file.stream)
            if img.mode != 'RGB': img = img.convert('RGB')
            img.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
            img.save(poster_path, 'JPEG', quality=82, optimize=True)
        else:
            placeholder = Image.new('RGB', (1280, 720), (200, 195, 185))
            placeholder.save(poster_path, 'JPEG', quality=70)
    except Exception as e:
        log.warning("poster fail: %s", e)
        placeholder = Image.new('RGB', (1280, 720), (200, 195, 185))
        placeholder.save(poster_path, 'JPEG', quality=70)

    duration_s = _safe_float(request.form.get('duration_s'))
    width = request.form.get('width')
    height = request.form.get('height')
    is_margin = request.form.get('is_margin') == '1'
    try:
        width = int(width) if width else None
        height = int(height) if height else None
    except ValueError:
        width = height = None

    rel_video = f"{c['couple_id']}/{final_name}"
    rel_poster = f"{c['couple_id']}/{poster_name}"

    # v4.4 : date de la video (lastModified cote client) -> classement chrono
    v_taken = (request.form.get('taken_at') or '').strip() or None
    vid = execute(
        "INSERT INTO videos (couple_id, file_path, poster_path, duration_s, "
        "width, height, taken_at, scan_token, added_by) VALUES (?,?,?,?,?,?,?,?,?)",
        (c['couple_id'], rel_video, rel_poster, duration_s, width, height,
         v_taken, token, session['uid'])
    )
    pos = _next_page_position(cid_carnet)
    page_id = execute(
        "INSERT INTO album_pages (carnet_id, type, position, video_id, "
        "is_margin, added_by) VALUES (?,?,?,?,?,?)",
        (cid_carnet, 'video', pos, vid, 1 if is_margin else 0, session['uid'])
    )
    try:
        _recompute_sections(cid_carnet)
    except Exception as e:
        log.warning("recompute fail: %s", e)
    return jsonify({
        'ok': True,
        'page_id': page_id, 'video_id': vid,
        'video_url': url_for('serve_upload', filename=rel_video),
        'poster_url': url_for('serve_upload', filename=rel_poster),
        'public_url': url_for('video_public', token=token, _external=True),
        'scan_token': token,
        'duration_s': duration_s,
        'is_margin': is_margin,
    })


@app.route('/carnet/<int:cid_carnet>/video_reclaim', methods=['POST'])
@couple_required
def carnet_video_reclaim(cid_carnet):
    """v4.4 : rattache a CET album une video orpheline de l'espace."""
    c = _get_carnet_or_404(cid_carnet)
    if not csrf_check():
        return jsonify({'ok': False, 'error': 'CSRF'}), 403
    vid = request.form.get('video_id')
    if not vid or not str(vid).isdigit():
        return jsonify({'ok': False, 'error': 'video_id'}), 400
    v = query("SELECT * FROM videos WHERE id=? AND couple_id=?",
              (int(vid), c['couple_id']), one=True)
    if not v:
        return jsonify({'ok': False, 'error': 'Video introuvable'}), 404
    exists = query("SELECT id FROM album_pages WHERE video_id=?", (v['id'],), one=True)
    if exists:
        return jsonify({'ok': False, 'error': 'Deja dans un album'}), 409
    # Sans date de prise, on approxime avec la date d'upload (classement chrono)
    if not v['taken_at'] and v['added_at']:
        execute("UPDATE videos SET taken_at=? WHERE id=?", (v['added_at'], v['id']))
    pos = _next_page_position(cid_carnet)
    page_id = execute(
        "INSERT INTO album_pages (carnet_id, type, position, video_id, "
        "is_margin, added_by) VALUES (?,?,?,?,0,?)",
        (cid_carnet, 'video', pos, v['id'], session['uid'])
    )
    try:
        _recompute_sections(cid_carnet)
    except Exception as e:
        log.warning("recompute apres reclaim: %s", e)
    return jsonify({'ok': True, 'page_id': page_id})


@app.route('/v/<token>')
def video_public(token):
    """Page publique de visionnage d'une video (deroulee depuis QR scan).
    Pas d'auth : le token est secret (urlsafe 12 caracteres)."""
    v = query("SELECT * FROM videos WHERE scan_token=?", (token,), one=True)
    if not v:
        return ("<html><body style='font-family:sans-serif;text-align:center;padding:60px'>"
                "<h2>Video introuvable</h2><p>Ce QR code n'est pas valide.</p></body></html>"), 404
    v = dict(v)
    return render_template('video_public.html', video=v, token=token)


@app.route('/v/<token>/file')
def video_public_file(token):
    """Stream de la video accessible via token (sans auth, comme la page)."""
    v = query("SELECT file_path, couple_id FROM videos WHERE scan_token=?", (token,), one=True)
    if not v:
        abort(404)
    return send_from_directory(UPLOAD_DIR, v['file_path'], max_age=31536000)


@app.route('/v/<token>/poster')
def video_public_poster(token):
    v = query("SELECT poster_path FROM videos WHERE scan_token=?", (token,), one=True)
    if not v:
        abort(404)
    return send_from_directory(UPLOAD_DIR, v['poster_path'], max_age=31536000)


# ── Routes : auth ─────────────────────────────────────────────────────
@app.route('/login', methods=['GET', 'POST'])
def login():
    """Inscription + connexion sur le meme formulaire (distinction par email en BDD)."""
    next_url = request.args.get('next') or request.form.get('next') or '/'
    if request.method == 'POST':
        if not csrf_check():
            flash("Session expiree, recommencez.", "err")
            return redirect(url_for('login'))
        email = (request.form.get('email') or '').strip().lower()
        password = request.form.get('password') or ''
        display_name = (request.form.get('display_name') or '').strip()
        mode = request.form.get('mode')  # 'signup' ou 'login'
        if not email or not password:
            flash("Email et mot de passe requis.", "err")
            return render_template('login.html', email=email, next_url=next_url)
        existing = query("SELECT * FROM users WHERE email=?", (email,), one=True)
        if mode == 'signup':
            if existing:
                flash("Cet email a deja un compte. Connectez-vous.", "err")
                return render_template('login.html', email=email, next_url=next_url)
            if len(password) < 8:
                flash("Mot de passe : 8 caracteres minimum.", "err")
                return render_template('login.html', email=email, display_name=display_name, next_url=next_url)
            uid = execute(
                "INSERT INTO users (email, display_name, password_hash) VALUES (?,?,?)",
                (email, display_name or email.split('@')[0], hash_pw(password))
            )
            session['uid'] = uid
            session['pw_at'] = _maintenant()   # v5.4 : repere de revocation
            session.permanent = True  # Brief 08 §1 : rester connecte
            session.pop('couple_id', None); session.pop('espace_id', None)
            return redirect(next_url if next_url.startswith('/') else '/')
        else:  # login
            if not existing or not check_pw(password, existing['password_hash']):
                flash("Email ou mot de passe incorrect.", "err")
                return render_template('login.html', email=email, next_url=next_url)
            if existing['deleted_at']:
                flash("Compte supprime. Contactez le support pour restaurer (30j max).", "err")
                return render_template('login.html', email=email, next_url=next_url)
            session['uid'] = existing['id']
            session['pw_at'] = str(existing['pw_changed_at'] or _maintenant())[:19]
            session.permanent = True  # Brief 08 §1 : rester connecte
            # Brief 08 §2 : on n'auto-selectionne PAS un espace.
            # L'utilisateur passe par le selecteur (/espace/choisir) au prochain
            # appel a home() s'il a des espaces, ou vers onboarding sinon.
            session.pop('espace_id', None); session.pop('couple_id', None)
            return redirect(next_url if next_url.startswith('/') else '/')
    return render_template('login.html', next_url=next_url)


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


# ── Routes : onboarding couple ────────────────────────────────────────
ESPACE_KINDS = [
    ('couple', 'Couple'),
    ('amis',   'Amis'),
    ('famille','Famille'),
    ('solo',   'Solo'),
]

# Charte §1.2 — 12 accents pre-selectionnes, desatures et harmoniques
ACCENTS = [
    ('terracotta', 'Terracotta'),
    ('olive',      'Olive'),
    ('clay',       'Argile'),
    ('sage',       'Sauge'),
    ('dusk',       'Crepuscule'),
    ('plum',       'Prune'),
    ('sand',       'Sable'),
    ('moss',       'Mousse'),
    ('ink',        'Encre'),
    ('rose',       'Rose'),
    ('mustard',    'Moutarde'),
    ('stone',      'Pierre'),
]


@app.route('/onboarding/couple', methods=['GET', 'POST'])
@login_required
def onboarding_couple():
    """Creation du 1er espace par l'user. Redirige si deja dans un espace."""
    user = current_user()
    if current_espace_id():
        return redirect(url_for('home'))
    if request.method == 'POST':
        if not csrf_check():
            flash("Session expiree.", "err")
            return redirect(url_for('onboarding_couple'))
        name = (request.form.get('name') or '').strip()
        kind = (request.form.get('kind') or 'couple').strip()
        if kind not in dict(ESPACE_KINDS):
            kind = 'couple'
        cid = execute(
            "INSERT INTO couples (name, kind, created_by) VALUES (?,?,?)",
            (name, kind, user['id'])
        )
        execute("INSERT INTO espace_members (espace_id, user_id, role) VALUES (?,?,?)",
                (cid, user['id'], 'owner'))
        execute("UPDATE users SET couple_id=? WHERE id=?", (cid, user['id']))
        session['espace_id'] = cid
        session['couple_id'] = cid
        return redirect(url_for('invite_share'))
    return render_template('onboarding.html', user=user, kinds=ESPACE_KINDS)


@app.route('/espace/nouveau', methods=['GET', 'POST'])
@login_required
def espace_nouveau():
    """Creer un nouvel espace pour l'user (en plus de ses espaces existants)."""
    user = current_user()
    if request.method == 'POST':
        if not csrf_check():
            flash("Session expiree.", "err")
            return redirect(url_for('espace_nouveau'))
        name = (request.form.get('name') or '').strip()
        kind = (request.form.get('kind') or 'couple').strip()
        if kind not in dict(ESPACE_KINDS):
            kind = 'couple'
        cid = execute(
            "INSERT INTO couples (name, kind, created_by) VALUES (?,?,?)",
            (name, kind, user['id'])
        )
        execute("INSERT INTO espace_members (espace_id, user_id, role) VALUES (?,?,?)",
                (cid, user['id'], 'owner'))
        session['espace_id'] = cid
        session['couple_id'] = cid
        return redirect(url_for('invite_share'))
    return render_template('espace_nouveau.html', user=user, kinds=ESPACE_KINDS)


@app.route('/espace/switch', methods=['POST'])
@login_required
def espace_switch():
    """Bascule sur un autre espace dont l'user est membre."""
    if not csrf_check():
        return redirect(url_for('home'))
    eid = request.form.get('espace_id')
    try:
        eid = int(eid)
    except (TypeError, ValueError):
        return redirect(url_for('home'))
    if set_current_espace(eid):
        return redirect(url_for('home'))
    flash("Espace inaccessible.", "err")
    return redirect(url_for('home'))


@app.route('/espace/choisir', methods=['GET'])
@login_required
def espace_choisir():
    """Brief 08 §2 : selecteur d'espace explicite (pas de defaut au login).

    Affiche la liste des espaces de l'utilisateur. Le choix se fait
    via le formulaire POST de /espace/switch (deja existant).
    """
    user = current_user()
    espaces = user_espaces(user['id']) if user else []
    if not espaces:
        return redirect(url_for('onboarding_couple'))
    return render_template('espace_choisir.html', user=user, espaces=espaces)


@app.route('/espace/personnaliser', methods=['GET', 'POST'])
@couple_required
def espace_personnaliser():
    """Personnalisation de l'espace courant : nom + couleur d'accent."""
    eid = current_espace_id()
    esp = query("SELECT * FROM couples WHERE id=?", (eid,), one=True)
    if request.method == 'POST':
        if not csrf_check():
            flash("Session expiree.", "err")
            return redirect(url_for('espace_personnaliser'))
        name = (request.form.get('name') or '').strip()[:80]
        accent = (request.form.get('accent') or 'terracotta').strip()
        if accent not in dict(ACCENTS):
            accent = 'terracotta'
        execute("UPDATE couples SET name=?, accent=? WHERE id=?", (name, accent, eid))
        flash("Espace personnalise.", "ok")
        return redirect(url_for('espace_personnaliser'))
    return render_template('espace_personnaliser.html', espace=dict(esp) if esp else None)


@app.route('/espace/membres')
@couple_required
def espace_membres():
    """Liste les membres de l'espace courant + invitations actives."""
    eid = current_espace_id()
    members = query("""
        SELECT u.id, u.email, u.display_name, em.role, em.joined_at
        FROM espace_members em JOIN users u ON u.id = em.user_id
        WHERE em.espace_id = ?
        ORDER BY em.joined_at ASC
    """, (eid,))
    invitations = query("""
        SELECT * FROM invitations
        WHERE couple_id=? AND utilise=0 AND expires_at > ?
        ORDER BY created_at DESC
    """, (eid, datetime.utcnow().isoformat()))
    return render_template('espace_membres.html',
        members=[dict(m) for m in members],
        invitations=[dict(i) for i in invitations],
    )


@app.route('/invite/share')
@couple_required
def invite_share():
    """Genere (si besoin) un lien d'invitation pour l'espace courant."""
    cid = current_espace_id()
    # v4.4 : lien GENERAL multi-usage — reste valable jusqu'a expiration,
    # peu importe combien de personnes l'ont deja utilise.
    inv = query(
        "SELECT * FROM invitations WHERE couple_id=? AND expires_at > ? "
        "ORDER BY created_at DESC LIMIT 1",
        (cid, datetime.utcnow().isoformat()),
        one=True
    )
    if not inv:
        token = secrets.token_urlsafe(20)
        expires = (datetime.utcnow() + timedelta(days=INVITATION_TTL_DAYS)).isoformat()
        execute(
            "INSERT INTO invitations (token, couple_id, expires_at) VALUES (?,?,?)",
            (token, cid, expires)
        )
    else:
        token = inv['token']
    invite_url = url_for('invite_accept', token=token, _external=True)
    exp_row = query("SELECT expires_at FROM invitations WHERE token=?", (token,), one=True)
    return render_template(
        'invite_share.html',
        invite_url=invite_url,
        qr=qr_svg(invite_url),
        expires_at=(exp_row['expires_at'] if exp_row else '')[:10],
        couple=query("SELECT * FROM couples WHERE id=?", (cid,), one=True),
    )


@app.route('/invite/regenerer', methods=['POST'])
@couple_required
def invite_regenerer():
    """v4.4 : genere un NOUVEAU lien et invalide les anciens (securite)."""
    if not csrf_check():
        flash("Session expiree.", "err")
        return redirect(url_for('invite_share'))
    cid = current_espace_id()
    execute("UPDATE invitations SET expires_at=? WHERE couple_id=?",
            (datetime.utcnow().isoformat(), cid))
    token = secrets.token_urlsafe(20)
    expires = (datetime.utcnow() + timedelta(days=INVITATION_TTL_DAYS)).isoformat()
    execute("INSERT INTO invitations (token, couple_id, expires_at) VALUES (?,?,?)",
            (token, cid, expires))
    flash("Nouveau lien genere — les anciens ne fonctionnent plus.", "ok")
    return redirect(url_for('invite_share'))


@app.route('/invite/<token>', methods=['GET', 'POST'])
def invite_accept(token):
    """
    Landing pour rejoindre un espace via lien d'invitation.
    Multi-espaces : un user peut etre membre de plusieurs espaces, donc
    on l'AJOUTE comme membre (pas de blocage si deja dans un autre).
    """
    # v4.4 : multi-usage — seule l'expiration invalide le lien
    inv = query(
        "SELECT * FROM invitations WHERE token=? AND expires_at > ?",
        (token, datetime.utcnow().isoformat()),
        one=True
    )
    if not inv:
        return render_template('invite_invalid.html'), 410

    couple = query("SELECT * FROM couples WHERE id=?", (inv['couple_id'],), one=True)
    if not couple:
        return render_template('invite_invalid.html'), 410

    eid = inv['couple_id']
    user = current_user()

    # Cas 1 : user deja connecte → on l'ajoute simplement comme membre
    if user:
        if is_member(user['id'], eid):
            flash("Vous etes deja membre de cet espace.", "ok")
        else:
            execute("INSERT OR IGNORE INTO espace_members (espace_id, user_id, role) VALUES (?,?,?)",
                    (eid, user['id'], 'member'))
            # v4.4 : on ne grille plus le lien — il sert a inviter d'autres membres
        session['espace_id'] = eid
        session['couple_id'] = eid
        return redirect(url_for('home'))

    # Cas 2 : user non connecte → signup ou login
    if request.method == 'POST':
        if not csrf_check():
            flash("Session expiree.", "err")
            return redirect(url_for('invite_accept', token=token))
        email = (request.form.get('email') or '').strip().lower()
        password = request.form.get('password') or ''
        display_name = (request.form.get('display_name') or '').strip()
        if not email or not password:
            flash("Email et mot de passe requis.", "err")
            return render_template('invite_accept.html', couple=couple, token=token)
        if len(password) < 8:
            flash("Mot de passe : 8 caracteres minimum.", "err")
            return render_template('invite_accept.html', couple=couple, token=token, email=email, display_name=display_name)
        existing = query("SELECT * FROM users WHERE email=?", (email,), one=True)
        if existing:
            if not check_pw(password, existing['password_hash']):
                flash("Cet email existe deja. Le mot de passe ne correspond pas.", "err")
                return render_template('invite_accept.html', couple=couple, token=token, email=email)
            uid = existing['id']
        else:
            uid = execute(
                "INSERT INTO users (email, display_name, password_hash, couple_id) VALUES (?,?,?,?)",
                (email, display_name or email.split('@')[0], hash_pw(password), eid)
            )
        execute("INSERT OR IGNORE INTO espace_members (espace_id, user_id, role) VALUES (?,?,?)",
                (eid, uid, 'member'))
        # v4.4 : lien multi-usage, on ne le grille plus
        session['uid'] = uid
        session['pw_at'] = _maintenant()   # v5.4 : repere de revocation
        session['espace_id'] = eid
        session['couple_id'] = eid
        return redirect(url_for('home'))

    return render_template('invite_accept.html', couple=couple, token=token)


# ══════════════════════════════════════════════════════════════════════
#                v2.0 — HISTOIRE & CONVERSATIONS
# ══════════════════════════════════════════════════════════════════════

def _get_conversation(espace_id):
    """Retourne la conversation de l'espace, la cree si manquante."""
    r = query("SELECT * FROM conversations WHERE espace_id=?", (espace_id,), one=True)
    if r:
        return dict(r)
    cid = execute("INSERT INTO conversations (espace_id) VALUES (?)", (espace_id,))
    return {'id': cid, 'espace_id': espace_id, 'archive_imported_at': None,
            'archive_source': '', 'created_at': datetime.utcnow().isoformat()}


def _conversation_messages(conv_id):
    """Retourne tous les messages (archived + live) ordonnes par sent_at,
    avec infos chapitre + sender + thumb_path photo jointe, hors deleted_at."""
    rows = query("""
        SELECT m.*,
               c.title AS chapter_title, c.headline AS chapter_headline,
               c.date_label AS chapter_date_label, c.weekday_label AS chapter_weekday,
               c.featured_image_url AS chapter_image, c.image_caption AS chapter_caption,
               c.position AS chapter_position,
               u.display_name AS sender_name, u.avatar_b64 AS sender_avatar,
               p.thumb_path AS attached_photo_thumb,
               p.file_path  AS attached_photo_full
        FROM messages m
        LEFT JOIN chapters c ON c.id = m.chapter_id
        LEFT JOIN users u ON u.id = m.sender_id
        LEFT JOIN photos p ON (m.attachment_type='photo' AND CAST(m.attachment_ref AS INTEGER) = p.id)
        WHERE m.conversation_id = ? AND m.deleted_at IS NULL
        ORDER BY m.sent_at ASC, m.id ASC
    """, (conv_id,))
    return [dict(r) for r in rows]


@app.route('/histoire')
@couple_required
def histoire():
    """Fil unifie : archive (immuable) + conversation continue (live).
    Supporte ?q=texte pour filtrer les messages."""
    eid = current_espace_id()
    conv = _get_conversation(eid)
    q = (request.args.get('q') or '').strip()
    if q:
        like = f"%{q}%"
        rows = query("""
            SELECT m.*,
                   c.title AS chapter_title, c.headline AS chapter_headline,
                   c.date_label AS chapter_date_label, c.weekday_label AS chapter_weekday,
                   c.featured_image_url AS chapter_image, c.image_caption AS chapter_caption,
                   c.position AS chapter_position,
                   u.display_name AS sender_name, u.avatar_b64 AS sender_avatar,
                   p.thumb_path AS attached_photo_thumb,
                   p.file_path  AS attached_photo_full
            FROM messages m
            LEFT JOIN chapters c ON c.id = m.chapter_id
            LEFT JOIN users u ON u.id = m.sender_id
            LEFT JOIN photos p ON (m.attachment_type='photo' AND CAST(m.attachment_ref AS INTEGER) = p.id)
            WHERE m.conversation_id = ? AND m.deleted_at IS NULL
              AND (m.body LIKE ? OR m.sender_label LIKE ? OR u.display_name LIKE ?)
            ORDER BY m.sent_at ASC, m.id ASC
        """, (conv['id'], like, like, like))
        msgs = [dict(r) for r in rows]
    else:
        msgs = _conversation_messages(conv['id'])
    members = query("""
        SELECT u.id, u.display_name FROM espace_members em
        JOIN users u ON u.id = em.user_id
        WHERE em.espace_id = ? ORDER BY em.joined_at ASC
    """, (eid,))
    member_ids = [m['id'] for m in members]
    bubble_color = {}
    for i, mid in enumerate(member_ids):
        bubble_color[mid] = 'A' if i == 0 else ('B' if i == 1 else 'C')
    # Liste des carnets de l'espace pour la mention @carnet (datalist)
    carnets_ref = query(
        "SELECT id, title, type FROM carnets WHERE couple_id=? AND deleted_at IS NULL "
        "ORDER BY title", (eid,)
    )
    # ── v5.9 : LE FIL UNIFIE — d'abord les mots, puis les images ──────
    # La timeline fusionne messages, journees de photos (mosaique par jour,
    # tous carnets confondus), departs de voyage, et la date charniere.
    # En mode recherche (?q=), on reste sur les messages seuls.
    espace = current_espace()
    charniere = {
        'date': (espace['date_charniere'] or '') if espace else '',
        'titre': (espace['charniere_titre'] or '') if espace else '',
    }
    if charniere['date']:
        # grande date en toutes lettres, formatee ICI (le formateur JS
        # global des [data-iso] ne doit pas la reecrire en dd/mm hh:mm)
        brut = _format_day_fr(charniere['date'][:10])  # 'JEUDI 30 AVRIL'
        charniere['label'] = (brut.capitalize().split(' ')[0] + ' '
                              + ' '.join(brut.lower().split(' ')[1:])
                              + ' ' + charniere['date'][:4])
    fil_items = [{'kind': 'message', 'm': m,
                  'sort': (_norm_ts(m.get('sent_at'))[:10] or '0000', 0,
                           _norm_ts(m.get('sent_at')))} for m in msgs]
    if not q:
        # journees de photos : par jour, tous carnets de l'espace
        photo_rows = query("""
            SELECT p.id AS photo_id, p.thumb_path, p.taken_at,
                   ap.id AS page_id, ap.carnet_id, ca.title AS carnet_title
            FROM photos p
            JOIN album_pages ap ON ap.photo_id = p.id
            JOIN carnets ca ON ca.id = ap.carnet_id
            WHERE p.couple_id = ? AND p.taken_at IS NOT NULL
              AND COALESCE(ap.is_hidden, 0) = 0 AND ca.deleted_at IS NULL
            ORDER BY p.taken_at ASC, p.id ASC
        """, (eid,))
        days = {}
        for r in photo_rows:
            ts = _norm_ts(r['taken_at'])
            day = ts[:10]
            if len(day) != 10:
                continue
            days.setdefault(day, []).append(dict(r))
        for day, photos in days.items():
            carnet_ids = list(dict.fromkeys(p['carnet_id'] for p in photos))
            titres = list(dict.fromkeys(p['carnet_title'] for p in photos))
            fil_items.append({
                'kind': 'photo_day', 'day': day,
                'photos': photos[:12], 'total': len(photos),
                'carnet_id': carnet_ids[0], 'carnet_titles': titres,
                'sort': (day, 1, day),
            })
        # departs de voyage (carnets dates)
        for ca in query("""SELECT id, title, date_start, date_end FROM carnets
                           WHERE couple_id=? AND deleted_at IS NULL
                             AND date_start IS NOT NULL AND date_start != ''""", (eid,)):
            day = str(ca['date_start'])[:10]
            fil_items.append({'kind': 'carnet_start', 'day': day,
                              'carnet': dict(ca), 'sort': (day, -1, day)})
        if charniere['date']:
            day = charniere['date'][:10]
            fil_items.append({'kind': 'charniere', 'day': day,
                              'sort': (day, -2, day)})
    fil_items.sort(key=lambda it: it['sort'])
    return render_template('histoire.html',
        conv=conv, messages=msgs, members=[dict(m) for m in members],
        bubble_color=bubble_color, query=q,
        fil_items=fil_items, charniere=charniere,
        carnets_ref=[dict(c) for c in carnets_ref]
    )


@app.route('/espace/charniere', methods=['POST'])
@couple_required
def espace_charniere():
    """v5.9 — Pose (ou corrige) la date charniere du recit : le jour ou
    la conversation laisse la place aux photos. Un titre court optionnel
    (« Lisbonne » suffit)."""
    if not csrf_check():
        return jsonify({'ok': False, 'error': 'CSRF'}), 403
    date = (request.form.get('date') or '').strip()
    titre = (request.form.get('titre') or '').strip()[:80]
    if date:
        try:
            datetime.strptime(date, '%Y-%m-%d')
        except ValueError:
            return jsonify({'ok': False, 'error': 'bad_date'}), 400
    execute("UPDATE couples SET date_charniere=?, charniere_titre=? WHERE id=?",
            (date, titre, current_espace_id()))
    _log_activity(current_espace_id(), session['uid'], 'charniere_set',
                  payload={'date': date, 'titre': titre})
    return jsonify({'ok': True})


@app.route('/histoire/message', methods=['POST'])
@couple_required
def histoire_post_message():
    if not csrf_check():
        return jsonify({'ok': False, 'error': 'CSRF'}), 403
    eid = current_espace_id()
    conv = _get_conversation(eid)
    body = (request.form.get('body') or '').strip()
    photo_file = request.files.get('photo')
    photo_path = None
    photo_thumb = None
    attachment_type = None
    attachment_ref = None

    if photo_file and photo_file.filename:
        try:
            data = _save_uploaded_photo(photo_file, eid)
            ct = request.form.get('photo_taken_at') or ''
            if ct and ct != 'null' and not data.get('taken_at'):
                data['taken_at'] = ct
            gps_lat = data.get('gps_lat') or _safe_float(request.form.get('photo_gps_lat'))
            gps_lng = data.get('gps_lng') or _safe_float(request.form.get('photo_gps_lng'))
            _inject_exif_to_jpeg(os.path.join(UPLOAD_DIR, data['file_path']),
                                 data.get('taken_at'), gps_lat, gps_lng)
            _inject_exif_to_jpeg(os.path.join(UPLOAD_DIR, data['thumb_path']),
                                 data.get('taken_at'), gps_lat, gps_lng)
            photo_id = execute(
                "INSERT INTO photos (couple_id, file_path, thumb_path, width, height, "
                "taken_at, gps_lat, gps_lng, added_by) VALUES (?,?,?,?,?,?,?,?,?)",
                (eid, data['file_path'], data['thumb_path'],
                 data['width'], data['height'], data['taken_at'],
                 gps_lat, gps_lng, session['uid'])
            )
            _enrich_photo_geo(photo_id, gps_lat, gps_lng)
            attachment_type = 'photo'
            attachment_ref = str(photo_id)
        except Exception as e:
            log.error("histoire photo upload: %s", e)
            return jsonify({'ok': False, 'error': 'Photo : ' + str(e)}), 500

    if not body and not attachment_type:
        return jsonify({'ok': False, 'error': 'Message vide'}), 400

    sent_at = datetime.utcnow().isoformat() + 'Z'
    user = current_user()
    mid = execute(
        "INSERT INTO messages (conversation_id, kind, sender_type, sender_id, sender_label, "
        "body, attachment_type, attachment_ref, sent_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (conv['id'], 'live', 'member', session['uid'], user['display_name'],
         body, attachment_type, attachment_ref, sent_at)
    )
    # Notif push aux autres membres de l'espace
    try:
        preview = (body[:80] + '...') if len(body) > 80 else (body or '📷 Photo')
        _notify_espace(eid, session['uid'], {
            'title': f"{user['display_name']} — Notre Histoire",
            'body': preview,
            'url': url_for('histoire'),
        })
    except Exception as e:
        log.warning("notify push echec: %s", e)
    return jsonify({'ok': True, 'message_id': mid, 'sent_at': sent_at})


@app.route('/histoire/message/<int:msg_id>/modifier', methods=['POST'])
@couple_required
def histoire_message_modifier(msg_id):
    if not csrf_check():
        return jsonify({'ok': False, 'error': 'CSRF'}), 403
    msg = query("SELECT m.*, c.espace_id FROM messages m "
                "JOIN conversations c ON c.id=m.conversation_id WHERE m.id=?",
                (msg_id,), one=True)
    if not msg or msg['espace_id'] != current_espace_id():
        return jsonify({'ok': False, 'error': '404'}), 404
    if msg['kind'] == 'archived':
        return jsonify({'ok': False, 'error': "L'archive est immuable"}), 403
    if msg['sender_id'] != session['uid']:
        return jsonify({'ok': False, 'error': "Auteur seulement"}), 403
    body = (request.form.get('body') or '').strip()
    if not body:
        return jsonify({'ok': False, 'error': 'Vide'}), 400
    execute("UPDATE messages SET body=?, edited_at=CURRENT_TIMESTAMP WHERE id=?",
            (body, msg_id))
    return jsonify({'ok': True})


@app.route('/histoire/message/<int:msg_id>/supprimer', methods=['POST'])
@couple_required
def histoire_message_supprimer(msg_id):
    if not csrf_check():
        return jsonify({'ok': False, 'error': 'CSRF'}), 403
    msg = query("SELECT m.*, c.espace_id FROM messages m "
                "JOIN conversations c ON c.id=m.conversation_id WHERE m.id=?",
                (msg_id,), one=True)
    if not msg or msg['espace_id'] != current_espace_id():
        return jsonify({'ok': False, 'error': '404'}), 404
    if msg['kind'] == 'archived':
        return jsonify({'ok': False, 'error': "L'archive est immuable"}), 403
    if msg['sender_id'] != session['uid']:
        return jsonify({'ok': False, 'error': "Auteur seulement"}), 403
    execute("UPDATE messages SET deleted_at=CURRENT_TIMESTAMP WHERE id=?", (msg_id,))
    return jsonify({'ok': True})


# ══════════════════════════════════════════════════════════════════════
#         v2.3 — ALGORITHME DE REGROUPEMENT CHRONOLOGIQUE
# ══════════════════════════════════════════════════════════════════════
# Brief 05 §1 : SECTION (jour OU lieu) > SOUS-SECTION (l'inverse)
# Cas A : 1 lieu / N jours -> level 1 = lieu, level 2 = jour
# Cas B : N jours / N lieux (1 lieu/jour) -> level 1 = jour, level 2 = lieu
# Cas C : 1 jour / >= 2 lieux -> level 1 = jour, level 2 = lieu
# ══════════════════════════════════════════════════════════════════════

def _haversine_km(lat1, lng1, lat2, lng2):
    """Distance approximative en km entre 2 points GPS."""
    import math
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlng/2)**2
    return 2 * R * math.asin(math.sqrt(a))


def _location_key(photo):
    """Cle de regroupement par lieu : city_name si dispo, sinon GPS arrondi 0.05° (~5km)."""
    cn = (photo.get('city_name') or '').strip().lower()
    if cn:
        return cn
    lat, lng = photo.get('gps_lat'), photo.get('gps_lng')
    if lat is not None and lng is not None:
        return f"gps_{round(lat * 20) / 20:.2f}_{round(lng * 20) / 20:.2f}"
    return None


def _location_label(photo, key):
    """Libelle visible d'un lieu."""
    cn = (photo.get('city_name') or '').strip()
    if cn:
        return cn
    if photo.get('gps_lat') is not None:
        return f"{photo['gps_lat']:.3f},{photo['gps_lng']:.3f}"
    return "Lieu inconnu"


def _part_of_day(hour):
    """Brief 05 §3.2 : MATIN 5-12, APRES-MIDI 12-18, SOIREE 18-22, NUIT 22-5."""
    if 5 <= hour < 12:  return 'MATIN'
    if 12 <= hour < 18: return 'APRES-MIDI'
    if 18 <= hour < 22: return 'SOIREE'
    return 'NUIT'


def _format_day_fr(date_str):
    """Convertit '2026-05-04' en 'LUNDI 4 MAI'."""
    try:
        from datetime import datetime as _dt
        d = _dt.strptime(date_str, '%Y-%m-%d')
        DAYS = ['LUNDI','MARDI','MERCREDI','JEUDI','VENDREDI','SAMEDI','DIMANCHE']
        MONTHS = ['JANVIER','FEVRIER','MARS','AVRIL','MAI','JUIN','JUILLET','AOUT','SEPTEMBRE','OCTOBRE','NOVEMBRE','DECEMBRE']
        return f"{DAYS[d.weekday()]} {d.day} {MONTHS[d.month-1]}"
    except Exception:
        return date_str


def _backfill_missing_geo(carnet_id, max_per_call=5):
    """Brief 08 §3 : enrichit les photos qui ont des coords GPS mais pas
    de city_name. Limite par appel pour respecter Nominatim (1 req/s).
    Best effort, silencieux."""
    try:
        rows = query("""
            SELECT p.id, p.gps_lat, p.gps_lng FROM photos p
            JOIN album_pages ap ON ap.photo_id = p.id
            WHERE ap.carnet_id = ?
              AND p.gps_lat IS NOT NULL AND p.gps_lng IS NOT NULL
              AND COALESCE(p.city_name, '') = ''
              AND COALESCE(p.address_full, '') = ''
            LIMIT ?
        """, (carnet_id, max_per_call))
        for r in rows:
            _enrich_photo_geo(r['id'], r['gps_lat'], r['gps_lng'])
    except Exception as e:
        log.debug("backfill geo fail: %s", e)


def _recompute_sections(carnet_id):
    """Recalcule les album_sections pour un carnet (idempotent).
    Preserve les pages avec manual_order=1 a leur position actuelle."""
    # Best-effort backfill geocoding pour les photos sans city_name
    _backfill_missing_geo(carnet_id)
    photos = query("""
        SELECT ap.id AS page_id, ap.position, ap.manual_order,
               p.taken_at, p.gps_lat, p.gps_lng, p.city_name,
               v.taken_at AS video_taken_at
        FROM album_pages ap
        LEFT JOIN photos p ON p.id = ap.photo_id
        LEFT JOIN videos v ON v.id = ap.video_id
        WHERE ap.carnet_id = ? AND COALESCE(ap.is_hidden, 0) = 0
    """, (carnet_id,))
    items = []
    for r in photos:
        r = dict(r)
        # v3.4 : normalisation du timestamp (formats mixtes T/espace/Z)
        ts = _norm_ts(r.get('taken_at') or r.get('video_taken_at'))
        if not ts:
            continue
        # Date locale (cle normalisee : 10 premiers chars)
        day = ts[:10]
        if not day or len(day) != 10:
            continue
        items.append({
            'page_id': r['page_id'],
            'taken_at': ts,
            'day': day,
            'gps_lat': r.get('gps_lat'),
            'gps_lng': r.get('gps_lng'),
            'city_name': r.get('city_name') or '',
            'manual_order': r.get('manual_order') or 0,
        })
    if not items:
        # Reset sections + section page id sur album_pages
        execute("DELETE FROM album_sections WHERE carnet_id=?", (carnet_id,))
        execute("UPDATE album_pages SET section_id=NULL WHERE carnet_id=?", (carnet_id,))
        return

    # Tri chronologique
    items.sort(key=lambda x: x['taken_at'])

    # Groupement par jour, puis par lieu dans chaque jour
    days = {}  # day -> list of items
    for it in items:
        days.setdefault(it['day'], []).append(it)

    # Determiner le cas A/B/C
    nb_days = len(days)
    all_locs = set()
    multi_loc_days = 0
    for day, day_items in days.items():
        locs_in_day = set(_location_key(p) for p in day_items if _location_key(p))
        if len(locs_in_day) >= 2:
            multi_loc_days += 1
        all_locs |= locs_in_day
    nb_locs = len(all_locs)

    if multi_loc_days >= 1:
        case = 'C'
    elif nb_locs <= 1 and nb_days >= 2:
        case = 'A'
    else:
        case = 'B'

    # Reset
    execute("DELETE FROM album_sections WHERE carnet_id=?", (carnet_id,))

    pos1 = 0  # position des sections niveau 1

    if case == 'A':
        # 1 lieu / N jours : level 1 = lieu, level 2 = jour
        first_loc_item = next((p for p in items if _location_key(p)), None)
        loc_label = _location_label(first_loc_item, _location_key(first_loc_item)) if first_loc_item else "Voyage"
        sec1_id = execute(
            "INSERT INTO album_sections (carnet_id, level, kind, primary_label, "
            "secondary_label, date_start, date_end, location_name, location_lat, "
            "location_lng, photo_count, position) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (carnet_id, 1, 'location', loc_label.upper(),
             f"{len(days)} jour(s) · {len(items)} photo(s)",
             items[0]['taken_at'], items[-1]['taken_at'], loc_label,
             first_loc_item['gps_lat'] if first_loc_item else None,
             first_loc_item['gps_lng'] if first_loc_item else None,
             len(items), pos1)
        )
        pos1 += 1
        pos2 = 0
        for day in sorted(days.keys()):
            day_items = days[day]
            sec2_id = execute(
                "INSERT INTO album_sections (carnet_id, level, parent_section_id, "
                "kind, primary_label, secondary_label, date_start, date_end, "
                "photo_count, position) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (carnet_id, 2, sec1_id, 'day', _format_day_fr(day),
                 f"{len(day_items)} photo(s)",
                 day_items[0]['taken_at'], day_items[-1]['taken_at'],
                 len(day_items), pos2)
            )
            pos2 += 1
            for it in day_items:
                if not it['manual_order']:
                    execute("UPDATE album_pages SET section_id=? WHERE id=?",
                            (sec2_id, it['page_id']))
    else:
        # Cas B ou C : level 1 = jour, level 2 = lieu
        for day in sorted(days.keys()):
            day_items = days[day]
            locs_in_day_label = sorted(set(
                _location_label(p, _location_key(p))
                for p in day_items if _location_key(p)
            ))
            sec1_id = execute(
                "INSERT INTO album_sections (carnet_id, level, kind, primary_label, "
                "secondary_label, date_start, date_end, photo_count, position) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (carnet_id, 1, 'day', _format_day_fr(day),
                 (', '.join(locs_in_day_label) + ' · ' if locs_in_day_label else '') +
                    f"{len(day_items)} photo(s)",
                 day_items[0]['taken_at'], day_items[-1]['taken_at'],
                 len(day_items), pos1)
            )
            pos1 += 1
            # Sous-sections par lieu (chrono dans le jour)
            current_key = None
            current_bucket = []
            buckets = []
            for it in day_items:
                k = _location_key(it)
                if k != current_key:
                    if current_bucket:
                        buckets.append((current_key, current_bucket))
                    current_key = k
                    current_bucket = [it]
                else:
                    current_bucket.append(it)
            if current_bucket:
                buckets.append((current_key, current_bucket))
            pos2 = 0
            for key, bucket in buckets:
                first = bucket[0]
                from datetime import datetime as _dt
                try:
                    hour = int(str(first['taken_at'])[11:13])
                except Exception:
                    hour = 12
                pod = _part_of_day(hour)
                loc_label = _location_label(first, key) if key else "Lieu inconnu"
                start_t = str(bucket[0]['taken_at'])[11:16]
                end_t = str(bucket[-1]['taken_at'])[11:16]
                sec2_id = execute(
                    "INSERT INTO album_sections (carnet_id, level, parent_section_id, "
                    "kind, primary_label, secondary_label, part_of_day, date_start, "
                    "date_end, location_name, location_lat, location_lng, "
                    "photo_count, position) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (carnet_id, 2, sec1_id, 'location',
                     f"{pod} · {loc_label}", f"{start_t} → {end_t} · {len(bucket)} photo(s)",
                     pod, bucket[0]['taken_at'], bucket[-1]['taken_at'],
                     loc_label, first.get('gps_lat'), first.get('gps_lng'),
                     len(bucket), pos2)
                )
                pos2 += 1
                for it in bucket:
                    if not it['manual_order']:
                        execute("UPDATE album_pages SET section_id=? WHERE id=?",
                                (sec2_id, it['page_id']))


@app.route('/carnet/<int:cid_carnet>/sections/recompute', methods=['POST'])
@couple_required
def carnet_recompute_sections(cid_carnet):
    """Reset manualOrder + recalcul auto."""
    c = _get_carnet_or_404(cid_carnet)
    if not csrf_check():
        return jsonify({'ok': False, 'error': 'CSRF'}), 403
    execute("UPDATE album_pages SET manual_order=0 WHERE carnet_id=?", (cid_carnet,))
    _recompute_sections(cid_carnet)
    return jsonify({'ok': True})


# ══════════════════════════════════════════════════════════════════════
#                v2.2 — WEB PUSH NOTIFICATIONS (PWA)
# ══════════════════════════════════════════════════════════════════════

@app.route('/push/vapid-key')
def push_vapid_key():
    """Retourne la cle publique VAPID (utilisee par le client pour s'abonner)."""
    return jsonify({'public_key': VAPID_PUBLIC_KEY})


@app.route('/push/subscribe', methods=['POST'])
@login_required
def push_subscribe():
    if not csrf_check():
        return jsonify({'ok': False, 'error': 'CSRF'}), 403
    import json as _json
    raw = request.get_data(as_text=True) or '{}'
    try:
        data = _json.loads(raw)
    except Exception:
        return jsonify({'ok': False, 'error': 'JSON invalide'}), 400
    endpoint = data.get('endpoint')
    keys = data.get('keys') or {}
    p256dh = keys.get('p256dh')
    auth = keys.get('auth')
    if not endpoint or not p256dh or not auth:
        return jsonify({'ok': False, 'error': 'Donnees manquantes'}), 400
    ua = request.headers.get('User-Agent', '')[:200]
    eid = current_espace_id()
    # ON CONFLICT : update si endpoint deja la
    try:
        execute("INSERT INTO push_subscriptions (user_id, espace_id, endpoint, "
                "p256dh, auth, user_agent) VALUES (?,?,?,?,?,?)",
                (session['uid'], eid, endpoint, p256dh, auth, ua))
    except sqlite3.IntegrityError:
        execute("UPDATE push_subscriptions SET p256dh=?, auth=?, espace_id=?, "
                "user_agent=? WHERE user_id=? AND endpoint=?",
                (p256dh, auth, eid, ua, session['uid'], endpoint))
    return jsonify({'ok': True})


@app.route('/push/unsubscribe', methods=['POST'])
@login_required
def push_unsubscribe():
    if not csrf_check():
        return jsonify({'ok': False, 'error': 'CSRF'}), 403
    import json as _json
    raw = request.get_data(as_text=True) or '{}'
    try:
        data = _json.loads(raw)
    except Exception:
        return jsonify({'ok': False, 'error': 'JSON invalide'}), 400
    endpoint = data.get('endpoint')
    if not endpoint:
        return jsonify({'ok': False, 'error': 'endpoint requis'}), 400
    execute("DELETE FROM push_subscriptions WHERE user_id=? AND endpoint=?",
            (session['uid'], endpoint))
    return jsonify({'ok': True})


def _send_push(subscription_row, payload_dict):
    """Envoi un push WebPush. Silencieux en cas d'erreur, supprime si 410/404."""
    if not VAPID_PRIVATE_KEY:
        return False
    try:
        from pywebpush import webpush, WebPushException
        import json as _json
        webpush(
            subscription_info={
                'endpoint': subscription_row['endpoint'],
                'keys': {'p256dh': subscription_row['p256dh'], 'auth': subscription_row['auth']},
            },
            data=_json.dumps(payload_dict),
            vapid_private_key=VAPID_PRIVATE_KEY,
            vapid_claims={'sub': VAPID_SUBJECT},
            ttl=86400,
        )
        return True
    except Exception as e:
        msg = str(e)
        log.info("push send fail: %s", msg[:200])
        # 410 Gone / 404 Not Found -> abonnement expire, on supprime
        if '410' in msg or '404' in msg or 'expired' in msg.lower():
            try:
                execute("DELETE FROM push_subscriptions WHERE id=?", (subscription_row['id'],))
            except Exception:
                pass
        return False


def _notify_espace(espace_id, exclude_user_id, payload):
    """Envoi notif a tous les membres de l'espace (sauf l'expediteur)."""
    if not VAPID_PRIVATE_KEY:
        return
    subs = query("""
        SELECT ps.* FROM push_subscriptions ps
        JOIN espace_members em ON em.user_id = ps.user_id AND em.espace_id = ps.espace_id
        WHERE ps.espace_id = ? AND ps.user_id != ?
    """, (espace_id, exclude_user_id))
    for s in subs:
        try:
            _send_push(dict(s), payload)
        except Exception as e:
            log.warning("notify echec sub=%s: %s", s.get('id'), e)


def _parse_jsx_chapters(src):
    """Extrait CHAPTERS d'un .jsx style notre_histoire.jsx (Hinge mockup).
    Resout les references IMG_* / S / L / A et parse via json5."""
    import json5 as _json5
    import json as _json
    import re as _re

    # 1) Extraire les constantes IMG_*
    imgs = {}
    for m in _re.finditer(r'^const\s+(IMG_\w+)\s*=\s*"((?:\\.|[^"\\])*)"\s*;',
                          src, flags=_re.M):
        imgs[m.group(1)] = m.group(2)

    # 2) Extraire le bloc CHAPTERS = [ ... ];
    m = _re.search(r'^const\s+CHAPTERS\s*=\s*(\[.*?^\]);',
                   src, flags=_re.M | _re.S)
    if not m:
        raise ValueError("Pas de declaration `const CHAPTERS = [...]` dans le fichier")
    block = m.group(1)

    # 3) Resoudre les references IMG_* (par ordre decroissant de longueur
    #    pour eviter qu'IMG_X soit remplace par bout d'IMG_XYZ)
    for key in sorted(imgs.keys(), key=len, reverse=True):
        block = block.replace(key, _json.dumps(imgs[key]))

    # 4) Resoudre les references S/L/A juste apres `s:`
    block = _re.sub(r'(\bs\s*:\s*)([SAL])\b', r'\1"\2"', block)

    # 5) Parser avec json5 (tolere unquoted keys, trailing commas, single quotes)
    return _json5.loads(block)


def _import_chapters_into_conv(conv_id, chapters_data, source='hinge'):
    """Insere une liste de chapitres parses dans la conversation.
    Reset l'archive existante d'abord."""
    execute("DELETE FROM messages WHERE conversation_id=? AND kind='archived'", (conv_id,))
    execute("DELETE FROM chapters WHERE conversation_id=?", (conv_id,))
    sender_map = {
        'S': ('system', ''),
        'A': ('userA',  'Arthur'),
        'L': ('userB',  'Laurie'),
    }
    nb_chapters = 0
    nb_messages = 0
    for idx, chap in enumerate(chapters_data):
        cap_id = execute(
            "INSERT INTO chapters (conversation_id, position, title, headline, "
            "date_label, weekday_label, featured_image_url, image_caption) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (conv_id, chap.get('id', idx + 1),
             chap.get('title', ''), chap.get('headline', ''),
             chap.get('date', '') or chap.get('dateLabel', ''),
             chap.get('weekday', '') or chap.get('weekdayLabel', ''),
             chap.get('image', '') or chap.get('featuredImageUrl', ''),
             chap.get('imageCaption', ''))
        )
        nb_chapters += 1
        # Date base : on parse `date` + `time` du chapitre, puis +1 min par message
        base_dt = None
        try:
            from datetime import datetime as _dt
            time_str = (chap.get('time') or '00:00').strip()
            # date ex "31 août" ; on garde un ISO synthetique avec annee 2025
            base_dt = _dt.strptime("2025 " + time_str, "%Y %H:%M")
            # Tente de parser le mois en francais (rough)
            base_dt = base_dt.replace(year=2025)
        except Exception:
            base_dt = None
        for j, msg in enumerate(chap.get('messages', [])):
            sender_code = msg.get('s') or msg.get('senderType', 'system')
            sender_type, sender_label = sender_map.get(sender_code, (sender_code, ''))
            if not sender_label:
                sender_label = msg.get('senderLabel', '')
            body = msg.get('t') or msg.get('body', '')
            sent_at = msg.get('sentAt')
            if not sent_at and base_dt:
                from datetime import timedelta as _td
                sent_at = (base_dt + _td(minutes=j)).isoformat()
            elif not sent_at:
                sent_at = datetime.utcnow().isoformat() + 'Z'
            execute(
                "INSERT INTO messages (conversation_id, kind, chapter_id, "
                "sender_type, sender_label, body, sent_at) VALUES (?,?,?,?,?,?,?)",
                (conv_id, 'archived', cap_id, sender_type, sender_label, body, sent_at)
            )
            nb_messages += 1
    execute(
        "UPDATE conversations SET archive_imported_at=CURRENT_TIMESTAMP, "
        "archive_source=? WHERE id=?",
        (source, conv_id)
    )
    return nb_chapters, nb_messages


@app.route('/histoire/import-jsx', methods=['POST'])
@couple_required
def histoire_import_jsx():
    """Upload du fichier notre_histoire.jsx -> import direct dans Histoire.
    Resout les references IMG_*, S/L/A automatiquement."""
    eid = current_espace_id()
    conv = _get_conversation(eid)
    if not csrf_check():
        flash("Session expiree.", "err")
        return redirect(url_for('histoire_import'))
    f = request.files.get('jsx_file')
    if not f or not f.filename:
        flash("Aucun fichier .jsx selectionne.", "err")
        return redirect(url_for('histoire_import'))
    try:
        src = f.stream.read().decode('utf-8', errors='replace')
    except Exception as e:
        flash(f"Lecture impossible : {e}", "err")
        return redirect(url_for('histoire_import'))
    try:
        chapters_data = _parse_jsx_chapters(src)
    except Exception as e:
        log.error("parse jsx echec: %s", e)
        flash(f"Parsing echoue : {e}", "err")
        return redirect(url_for('histoire_import'))
    try:
        nb_chap, nb_msg = _import_chapters_into_conv(conv['id'], chapters_data, source='hinge')
    except Exception as e:
        log.error("import chapters echec: %s\n%s", e, traceback.format_exc())
        flash(f"Import echoue : {e}", "err")
        return redirect(url_for('histoire_import'))
    flash(f"Archive importee depuis {f.filename} : {nb_chap} chapitre(s), {nb_msg} message(s).", "ok")
    return redirect(url_for('histoire'))


@app.route('/histoire/import', methods=['GET', 'POST'])
@couple_required
def histoire_import():
    """Import d'une archive de conversation au format JSON (cf. brief V2 §22)."""
    eid = current_espace_id()
    conv = _get_conversation(eid)
    if request.method == 'POST':
        if not csrf_check():
            flash("Session expiree.", "err")
            return redirect(url_for('histoire_import'))
        import json as _json
        raw = request.form.get('archive_json') or ''
        try:
            data = _json.loads(raw)
        except Exception as e:
            flash(f"JSON invalide : {e}", "err")
            return render_template('histoire_import.html', conv=conv, raw=raw)
        # Validation minimale
        if not isinstance(data, dict) or 'chapters' not in data:
            flash("Format invalide : il manque la cle 'chapters'.", "err")
            return render_template('histoire_import.html', conv=conv, raw=raw)
        # Reset archive existante (chapitres + messages archived)
        execute("DELETE FROM messages WHERE conversation_id=? AND kind='archived'", (conv['id'],))
        execute("DELETE FROM chapters WHERE conversation_id=?", (conv['id'],))
        # Import
        nb_chapters = 0
        nb_messages = 0
        for chap in data.get('chapters', []):
            cap_id = execute(
                "INSERT INTO chapters (conversation_id, position, title, headline, "
                "date_label, weekday_label, featured_image_url, image_caption) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (conv['id'], chap.get('position', nb_chapters),
                 chap.get('title', ''), chap.get('headline', ''),
                 chap.get('dateLabel', ''), chap.get('weekdayLabel', ''),
                 chap.get('featuredImageUrl', ''), chap.get('imageCaption', ''))
            )
            nb_chapters += 1
            for msg in chap.get('messages', []):
                execute(
                    "INSERT INTO messages (conversation_id, kind, chapter_id, "
                    "sender_type, sender_label, body, sent_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (conv['id'], 'archived', cap_id,
                     msg.get('senderType', 'system'),
                     msg.get('senderLabel', ''),
                     msg.get('body', ''),
                     msg.get('sentAt', datetime.utcnow().isoformat()))
                )
                nb_messages += 1
        execute(
            "UPDATE conversations SET archive_imported_at=CURRENT_TIMESTAMP, "
            "archive_source=? WHERE id=?",
            (data.get('source', 'manual'), conv['id'])
        )
        flash(f"Archive importee : {nb_chapters} chapitre(s), {nb_messages} message(s).", "ok")
        return redirect(url_for('histoire'))
    return render_template('histoire_import.html', conv=conv)


# ══════════════════════════════════════════════════════════════════════
#                       v1.6 — PROFIL UTILISATEUR
# ══════════════════════════════════════════════════════════════════════

@app.route('/profil')
@login_required
def profil():
    """Brief 08 §4 : page profil hardenee — chaque etape echoue silencieusement
    pour eviter le 500 quand un compte est dans un etat partiel
    (uid en session sans user en base, espace orphelin, etc.)."""
    try:
        user = current_user()
        if not user:
            # uid en session mais user disparu → on nettoie et on redirige
            session.clear()
            return redirect(url_for('login'))
        try:
            espaces = user_espaces(user['id']) or []
        except Exception as e:
            log.warning("profil: user_espaces fail: %s", e)
            espaces = []
        stats = {'carnets': 0, 'photos': 0, 'reveries': 0, 'videos': 0}
        try:
            eid = current_espace_id()
            if eid:
                r = query("SELECT COUNT(*) AS n FROM carnets WHERE couple_id=? "
                          "AND deleted_at IS NULL AND type != 'souhait'", (eid,), one=True)
                stats['carnets'] = r['n'] if r else 0
                r = query("SELECT COUNT(*) AS n FROM carnets WHERE couple_id=? "
                          "AND deleted_at IS NULL AND type = 'souhait'", (eid,), one=True)
                stats['reveries'] = r['n'] if r else 0
            r = query("SELECT COUNT(*) AS n FROM photos WHERE added_by=?", (user['id'],), one=True)
            stats['photos'] = r['n'] if r else 0
            r = query("SELECT COUNT(*) AS n FROM videos WHERE added_by=?", (user['id'],), one=True)
            stats['videos'] = r['n'] if r else 0
        except Exception as e:
            log.warning("profil: stats fail: %s", e)
        return render_template('profil.html', user=user, espaces=espaces, stats=stats)
    except Exception as e:
        log.error("profil ECHEC: %s\n%s", e, traceback.format_exc())
        flash("Profil indisponible pour le moment. Erreur : %s" % e, "err")
        return redirect(url_for('home'))


@app.route('/profil/displayname', methods=['POST'])
@login_required
def profil_displayname():
    if not csrf_check():
        flash("Session expiree.", "err")
        return redirect(url_for('profil'))
    name = (request.form.get('display_name') or '').strip()
    if not name:
        flash("Prenom requis.", "err")
        return redirect(url_for('profil'))
    if len(name) > 60:
        flash("Prenom trop long (max 60).", "err")
        return redirect(url_for('profil'))
    execute("UPDATE users SET display_name=? WHERE id=?", (name, session['uid']))
    flash("Prenom mis a jour.", "ok")
    return redirect(url_for('profil'))


@app.route('/profil/avatar', methods=['POST'])
@login_required
def profil_avatar():
    if not csrf_check():
        return jsonify({'ok': False, 'error': 'CSRF'}), 403
    f = request.files.get('avatar')
    if not f or not f.filename:
        return jsonify({'ok': False, 'error': 'Aucun fichier'}), 400
    try:
        img = Image.open(f.stream)
        if img.mode in ('RGBA', 'LA', 'P'):
            bg = Image.new('RGB', img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[-1] if img.mode in ('RGBA', 'LA') else None)
            img = bg
        elif img.mode != 'RGB':
            img = img.convert('RGB')
        # Crop carre central
        w, h = img.size
        side = min(w, h)
        left = (w - side) // 2
        top = (h - side) // 2
        img = img.crop((left, top, left + side, top + side))
        img.thumbnail((200, 200), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, 'JPEG', quality=82, optimize=True)
        b64 = "data:image/jpeg;base64," + __import__('base64').b64encode(buf.getvalue()).decode()
        execute("UPDATE users SET avatar_b64=? WHERE id=?", (b64, session['uid']))
        return jsonify({'ok': True, 'avatar_url': b64})
    except Exception as e:
        log.error("avatar fail: %s", e)
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/profil/avatar/supprimer', methods=['POST'])
@login_required
def profil_avatar_supprimer():
    if not csrf_check():
        flash("Session expiree.", "err")
        return redirect(url_for('profil'))
    execute("UPDATE users SET avatar_b64='' WHERE id=?", (session['uid'],))
    flash("Avatar retire.", "ok")
    return redirect(url_for('profil'))


@app.route('/profil/quitter/<int:eid>', methods=['POST'])
@login_required
def profil_quitter_espace(eid):
    if not csrf_check():
        flash("Session expiree.", "err")
        return redirect(url_for('profil'))
    uid = session['uid']
    if not is_member(uid, eid):
        abort(404)
    # Compte les autres membres
    others = query("SELECT COUNT(*) AS n FROM espace_members WHERE espace_id=? AND user_id != ?",
                   (eid, uid), one=True)
    nb_others = others['n'] if others else 0
    execute("DELETE FROM espace_members WHERE espace_id=? AND user_id=?", (eid, uid))
    if session.get('espace_id') == eid:
        # Bascule sur un autre espace si dispo
        esps = user_espaces(uid)
        if esps:
            session['espace_id'] = esps[0]['id']
            session['couple_id'] = esps[0]['id']
        else:
            session.pop('espace_id', None)
            session.pop('couple_id', None)
    if nb_others == 0:
        flash("Tu as quitte cet espace. Personne d'autre n'y restait — les contenus sont conserves.", "ok")
    else:
        flash("Tu as quitte cet espace.", "ok")
    return redirect(url_for('profil'))


@app.route('/profil/supprimer', methods=['POST'])
@login_required
def profil_supprimer():
    if not csrf_check():
        flash("Session expiree.", "err")
        return redirect(url_for('profil'))
    confirm = request.form.get('confirm') or ''
    if confirm != 'SUPPRIMER':
        flash("Tape SUPPRIMER pour confirmer.", "err")
        return redirect(url_for('profil'))
    uid = session['uid']
    execute("UPDATE users SET deleted_at=CURRENT_TIMESTAMP WHERE id=?", (uid,))
    session.clear()
    flash("Compte supprime. Tu as 30 jours pour le recuperer (contact support).", "ok")
    return redirect(url_for('login'))


# ══════════════════════════════════════════════════════════════════════
#                      v1.4.3 — BACKUP AUTO BDD
# ══════════════════════════════════════════════════════════════════════

def _do_backup():
    """
    Cree un dump SQLite (atomique via SQLite backup API), le ZIP,
    applique la rotation, et envoie par email si SMTP configure.
    Retourne dict {filename, size, email_sent}.
    """
    ts = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
    sqlite_dump = os.path.join(BACKUP_DIR, f'carnet_{ts}.sqlite')
    zip_path = os.path.join(BACKUP_DIR, f'carnet_{ts}.zip')

    # Dump atomique (SQLite backup API : safe meme si ecritures concurrentes)
    src = sqlite3.connect(DB_PATH)
    dst = sqlite3.connect(sqlite_dump)
    src.backup(dst)
    dst.close()
    src.close()

    # ZIP
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.write(sqlite_dump, arcname=f'carnet_{ts}.sqlite')
    try:
        os.remove(sqlite_dump)
    except Exception:
        pass

    # Rotation
    backups = sorted([f for f in os.listdir(BACKUP_DIR)
                      if f.startswith('carnet_') and f.endswith('.zip')])
    while len(backups) > BACKUP_KEEP:
        oldest = backups.pop(0)
        try:
            os.remove(os.path.join(BACKUP_DIR, oldest))
        except Exception:
            pass

    size = os.path.getsize(zip_path)
    log.info("backup cree : %s (%d bytes)", os.path.basename(zip_path), size)

    # Email si SMTP configure
    sent = False
    if SMTP_HOST and SMTP_USER and BACKUP_EMAIL_TO:
        try:
            msg = MIMEMultipart()
            msg['From'] = SMTP_FROM or SMTP_USER
            msg['To'] = BACKUP_EMAIL_TO
            msg['Subject'] = f'[Notre Histoire] Backup BDD du {ts}'
            msg.attach(MIMEText(
                f"Backup automatique de la base SQLite.\n"
                f"Date : {ts} UTC\n"
                f"Taille : {size / 1024:.1f} Ko\n"
                f"Fichier : carnet_{ts}.zip\n",
                'plain'
            ))
            with open(zip_path, 'rb') as fp:
                attach = MIMEApplication(fp.read(), _subtype='zip')
                attach.add_header('Content-Disposition', 'attachment',
                                  filename=f'carnet_{ts}.zip')
                msg.attach(attach)
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
                smtp.starttls()
                smtp.login(SMTP_USER, SMTP_PASS)
                smtp.send_message(msg)
            sent = True
            log.info("backup email envoye a %s", BACKUP_EMAIL_TO)
        except Exception as e:
            log.warning("backup email ECHEC: %s", e)

    return {
        'filename': os.path.basename(zip_path),
        'size': size,
        'email_sent': sent,
    }


@app.route('/admin/backup/run', methods=['GET', 'POST'])
def admin_backup_run():
    """
    Declenche un backup. Auth : token (?token=XXX) pour cron externe,
    ou user logged (admin manuel via UI).
    """
    token = (request.args.get('token') or
             request.headers.get('X-Backup-Token') or
             request.form.get('token') or '')
    if BACKUP_TOKEN and token == BACKUP_TOKEN:
        pass
    elif session.get('uid'):
        u = current_user()
        if not u or (u.get('email') or '').lower() not in ADMIN_EMAILS:
            abort(403)
    else:
        abort(403)
    try:
        result = _do_backup()
        return jsonify({'ok': True, **result})
    except Exception as e:
        log.error("backup ECHEC: %s\n%s", e, traceback.format_exc())
        return jsonify({'ok': False, 'error': str(e)}), 500


# ══════════════════════════════════════════════════════════════════════
#          v5.4 — ESPACE ADMIN : LES COMPTES ET LEURS MOTS DE PASSE
# ══════════════════════════════════════════════════════════════════════

@app.route('/admin')
@admin_required
def admin_home():
    """Le tableau de bord de l'espace admin de Carnet.
    (AqGK a le sien, chez lui : deux apps, deux bases, deux espaces.)"""
    stats = query("""
        SELECT (SELECT COUNT(*) FROM users WHERE deleted_at IS NULL)   AS n_users,
               (SELECT COUNT(*) FROM couples)                          AS n_espaces,
               (SELECT COUNT(*) FROM carnets WHERE deleted_at IS NULL) AS n_carnets,
               (SELECT COUNT(*) FROM password_resets
                 WHERE used_at IS NULL AND expires_at > ?)             AS n_liens
    """, (_maintenant(),), one=True)
    return render_template('admin_home.html', stats=dict(stats) if stats else {})


@app.route('/admin/comptes')
@admin_required
def admin_comptes():
    """Liste des comptes, avec de quoi retrouver quelqu'un qui appelle."""
    q = (request.args.get('q') or '').strip()
    sql = """
        SELECT u.id, u.email, u.display_name, u.created_at, u.pw_changed_at,
               (SELECT COUNT(*) FROM espace_members em WHERE em.user_id = u.id) AS n_espaces
        FROM users u WHERE u.deleted_at IS NULL
    """
    params = []
    if q:
        sql += " AND (LOWER(u.email) LIKE ? OR LOWER(u.display_name) LIKE ?)"
        params += ['%' + q.lower() + '%', '%' + q.lower() + '%']
    sql += " ORDER BY u.created_at DESC LIMIT 200"
    comptes = [dict(r) for r in query(sql, tuple(params))]
    return render_template('admin_comptes.html', comptes=comptes, q=q,
                           lien=None, lien_pour=None, ttl=RESET_TTL_HEURES)


@app.route('/admin/comptes/<int:uid_cible>/lien', methods=['POST'])
@admin_required
def admin_compte_lien(uid_cible):
    """Cree le lien de reinitialisation. Il s'affiche UNE fois : ni la base ni
    les logs ne le reverront. S'il se perd, on en refait un (le precedent meurt)."""
    if not csrf_check():
        flash("Session expiree, recommencez.", "err")
        return redirect(url_for('admin_comptes'))
    u = query("SELECT id, email, display_name FROM users "
              "WHERE id=? AND deleted_at IS NULL", (uid_cible,), one=True)
    if not u:
        flash("Compte introuvable.", "err")
        return redirect(url_for('admin_comptes'))
    token = _reset_creer(u['id'], session.get('uid'))
    log.info("lien de reinitialisation cree pour le compte %s par %s",
             u['id'], session.get('uid'))       # jamais le jeton dans les logs
    comptes = [dict(r) for r in query("""
        SELECT u.id, u.email, u.display_name, u.created_at, u.pw_changed_at,
               (SELECT COUNT(*) FROM espace_members em WHERE em.user_id = u.id) AS n_espaces
        FROM users u WHERE u.deleted_at IS NULL ORDER BY u.created_at DESC LIMIT 200
    """)]
    return render_template('admin_comptes.html', comptes=comptes, q='',
                           lien=url_for('reset_password', token=token, _external=True),
                           lien_pour=dict(u), ttl=RESET_TTL_HEURES)


@app.route('/mot-de-passe-oublie', methods=['GET', 'POST'])
def mot_de_passe_oublie():
    """Ce que voit quelqu'un qui ne sait plus son mot de passe.

    Si l'envoi d'emails est branche, il demande son lien lui-meme et le
    recoit. Sinon, la page reste la notice : il passe par l'admin. On ne
    propose jamais un formulaire qui ne posterait nulle part."""
    contact = sorted(ADMIN_EMAILS)[0] if ADMIN_EMAILS else ''
    if request.method == 'POST' and mail_configure():
        if not csrf_check():
            flash("Session expiree, recommencez.", "err")
            return redirect(url_for('mot_de_passe_oublie'))
        email = (request.form.get('email') or '').strip().lower()
        u = query("SELECT id, email, display_name FROM users "
                  "WHERE LOWER(email)=? AND deleted_at IS NULL", (email,), one=True)
        if u:
            # Garde-fou : on ne transforme pas la page en machine a spammer
            recents = query(
                "SELECT COUNT(*) AS n FROM password_resets "
                "WHERE user_id=? AND created_at > datetime('now', '-1 hour')",
                (u['id'],), one=True)
            if (recents['n'] if recents else 0) < RESET_MAX_PAR_HEURE:
                token = _reset_creer(u['id'], None)
                lien = url_for('reset_password', token=token, _external=True)
                _envoyer_mail(u['email'], "Votre lien pour choisir un mot de passe",
                              _mail_reset_html(u['display_name'], lien))
            else:
                log.warning("trop de demandes de reinitialisation pour le compte %s", u['id'])
        # Reponse IDENTIQUE dans tous les cas : compte connu ou non, quota
        # atteint ou non. La page ne doit jamais reveler qui a un compte ici.
        return render_template('mot_de_passe_oublie.html', contact=contact,
                               par_mail=True, envoye=True)
    return render_template('mot_de_passe_oublie.html', contact=contact,
                           par_mail=mail_configure(), envoye=False)


@app.route('/reset/<token>', methods=['GET', 'POST'])
def reset_password(token):
    """Le lien recu. Une seule chose a faire : choisir un nouveau mot de passe."""
    u = _reset_lire(token)
    if not u:
        # inconnu, expire, deja servi : meme page, meme mot. On ne renseigne pas.
        return render_template('reset_password.html', invalide=True,
                               contact=sorted(ADMIN_EMAILS)[0] if ADMIN_EMAILS else ''), 400
    if request.method == 'POST':
        if not csrf_check():
            flash("Session expiree, recommencez.", "err")
            return render_template('reset_password.html', invalide=False, prenom=u['display_name'])
        mdp = request.form.get('password') or ''
        mdp2 = request.form.get('password2') or ''
        if len(mdp) < 8:
            flash("Le mot de passe doit faire 8 caracteres au minimum.", "err")
            return render_template('reset_password.html', invalide=False, prenom=u['display_name'])
        if mdp != mdp2:
            flash("Les deux mots de passe ne sont pas identiques.", "err")
            return render_template('reset_password.html', invalide=False, prenom=u['display_name'])
        if not _reset_consommer(token, u['id'], mdp):
            return render_template('reset_password.html', invalide=True,
                                   contact=sorted(ADMIN_EMAILS)[0] if ADMIN_EMAILS else ''), 400
        session.clear()        # on repart d'une page propre, y compris pour un intrus
        flash("Mot de passe enregistre. Connectez-vous avec le nouveau.", "ok")
        return redirect(url_for('login'))
    return render_template('reset_password.html', invalide=False, prenom=u['display_name'])


@app.route('/admin/backups')
@admin_required
def admin_backups_list():
    """Page admin : liste des backups + bouton 'creer maintenant'."""
    backups = []
    if os.path.isdir(BACKUP_DIR):
        for f in sorted(os.listdir(BACKUP_DIR), reverse=True):
            if f.startswith('carnet_') and f.endswith('.zip'):
                p = os.path.join(BACKUP_DIR, f)
                backups.append({
                    'name': f,
                    'size_kb': round(os.path.getsize(p) / 1024, 1),
                    'mtime': datetime.fromtimestamp(os.path.getmtime(p)).strftime('%Y-%m-%d %H:%M'),
                })
    return render_template('admin_backups.html',
        backups=backups,
        smtp_configured=bool(SMTP_HOST and SMTP_USER),
        backup_email=BACKUP_EMAIL_TO,
        backup_token_set=bool(BACKUP_TOKEN),
        backup_keep=BACKUP_KEEP,
    )


@app.route('/admin/backups/<path:filename>')
@admin_required
def admin_backup_download(filename):
    if not filename.startswith('carnet_') or not filename.endswith('.zip'):
        abort(404)
    return send_from_directory(BACKUP_DIR, filename, as_attachment=True)


@app.route('/admin/backups/<path:filename>/delete', methods=['POST'])
@admin_required
def admin_backup_delete(filename):
    if not csrf_check(): abort(403)
    if not filename.startswith('carnet_') or not filename.endswith('.zip'):
        abort(404)
    p = os.path.join(BACKUP_DIR, filename)
    if os.path.exists(p):
        os.remove(p)
        flash(f"{filename} supprime.", "ok")
    return redirect(url_for('admin_backups_list'))


@app.errorhandler(500)
def _on_500(e):
    """Brief 08 §4 : on log la trace complete pour diagnostic Railway,
    et on affiche un message moins anxiogene a l'utilisateur."""
    log.error("500 sur %s : %s\n%s", request.path, e, traceback.format_exc())
    return ("<!doctype html><meta charset=utf-8>"
            "<style>body{font:16px/1.5 system-ui;padding:24px;max-width:520px;margin:auto}"
            "a{color:#A8503D}</style>"
            "<h1>Oups, une erreur est survenue.</h1>"
            "<p>Reessayez ou revenez a <a href='/'>l'accueil</a>.</p>"), 500


# ── Bootstrap ─────────────────────────────────────────────────────────
init_db()

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=int(os.environ.get('PORT', 5050)))
