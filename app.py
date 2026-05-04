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
from flask import (
    Flask, render_template, request, redirect, url_for, session,
    jsonify, abort, flash, send_from_directory
)

logging.basicConfig(level=logging.INFO,
    format='[%(asctime)s] %(levelname)s %(name)s: %(message)s')
log = logging.getLogger('histoire')

# ── Config ────────────────────────────────────────────────────────────
APP_VERSION = "2.4.0-brief06"
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


# ── DB helpers ────────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


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
        # ── v2.3 — Mise en page (Brief 05 §14-17) ─────────────────────
        "ALTER TABLE carnets ADD COLUMN default_photos_per_page INTEGER DEFAULT 1",
        "ALTER TABLE carnets ADD COLUMN default_page_margin REAL DEFAULT 15.0",
        "ALTER TABLE album_pages ADD COLUMN photos_per_page_override INTEGER",
        "ALTER TABLE album_pages ADD COLUMN page_margin_override REAL",
        "ALTER TABLE album_pages ADD COLUMN full_bleed_override INTEGER",
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
    ]
    for sql in migrations:
        try:
            conn.execute(sql)
        except Exception:
            pass
    conn.commit()
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
    """Variables disponibles dans tous les templates."""
    u = current_user()
    espaces = user_espaces(u['id']) if u else []
    esp = current_espace()
    nb_souhaits = 0
    eid = current_espace_id()
    if eid:
        try:
            r = query("SELECT COUNT(*) AS n FROM carnets WHERE couple_id=? "
                      "AND type='souhait' AND deleted_at IS NULL", (eid,), one=True)
            nb_souhaits = r['n'] if r else 0
        except Exception:
            nb_souhaits = 0
    is_admin = bool(u and (u.get('email') or '').lower() in ADMIN_EMAILS)
    return {
        'current_user': u,
        'current_espace': esp,
        'current_accent': (esp.get('accent') if esp else 'terracotta') or 'terracotta',
        'user_espaces': espaces,
        'nb_souhaits': nb_souhaits,
        'is_admin': is_admin,
        'admin_emails': ADMIN_EMAILS,
        'csrf_token': csrf_token,
        'app_version': APP_VERSION,
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
    return render_template(
        'index.html',
        carnets=[dict(r) for r in rows],
        types=CARNET_TYPES,
        type_filter=type_filter,
        nb_souhaits=nb_souhaits,
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
            "SELECT COUNT(*) AS n FROM carnet_items WHERE carnet_id=? AND target_carnet_id IS NULL",
            (r['id'],), one=True
        )
        r['nb_items'] = cnt['n'] if cnt else 0
        souhaits.append(r)
    return render_template(
        'souhaits.html',
        souhaits=souhaits,
        kinds=SOUHAIT_KINDS,
        kind_filter=kind_filter,
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
               u.display_name AS added_by_name
        FROM carnet_items ci
        LEFT JOIN photos p ON p.id = ci.photo_id
        LEFT JOIN users u ON u.id = ci.added_by
        WHERE ci.carnet_id = ? AND ci.target_carnet_id IS NULL
        ORDER BY ci.position ASC, ci.id ASC
    """, (carnet_id,))
    return [dict(r) for r in rows]


def _next_item_position(carnet_id):
    r = query(
        "SELECT COALESCE(MAX(position), -1) + 1 AS next FROM carnet_items WHERE carnet_id=?",
        (carnet_id,), one=True
    )
    return r['next'] if r else 0


@app.route('/carnet/<int:cid_carnet>/souhait')
@couple_required
def carnet_souhait_view(cid_carnet):
    c = _get_carnet_or_404(cid_carnet)
    items = _carnet_items(cid_carnet)
    # Voyages issus de cette reverie
    voyages = query(
        "SELECT id, title, status, created_at FROM carnets "
        "WHERE parent_souhait_id=? AND deleted_at IS NULL ORDER BY created_at DESC",
        (cid_carnet,)
    )
    return render_template('carnet_souhait.html', carnet=c, items=items,
        voyages=[dict(v) for v in voyages], item_kinds=ITEM_KINDS)


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
        except Exception as e:
            log.error("upload item photo: %s", e)
            return jsonify({'ok': False, 'error': 'Photo : ' + str(e)}), 500
    pos = _next_item_position(cid_carnet)
    iid = execute(
        "INSERT INTO carnet_items (carnet_id, position, kind, title, body, url, "
        "photo_id, address, amount, currency, added_by) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (cid_carnet, pos, kind, title, body, url_v, photo_id, address,
         amount, currency, session['uid'])
    )
    return jsonify({'ok': True, 'item_id': iid})


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
    execute("DELETE FROM carnet_items WHERE id=?", (item_id,))
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
        selected_ids = [int(x) for x in request.form.getlist('item_ids') if str(x).isdigit()]
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
            if selected_ids:
                placeholders = ','.join('?' * len(selected_ids))
                if duplicate:
                    # Copier les items au lieu de les deplacer
                    conn.execute(
                        f"INSERT INTO carnet_items (carnet_id, position, kind, title, "
                        f"body, url, photo_id, address, geo_lat, geo_lng, amount, currency, added_by) "
                        f"SELECT ?, position, kind, title, body, url, photo_id, address, "
                        f"geo_lat, geo_lng, amount, currency, added_by "
                        f"FROM carnet_items WHERE id IN ({placeholders})",
                        tuple([new_cid] + selected_ids)
                    )
                else:
                    # Deplacer : changer carnet_id (le souhait n'a plus l'item)
                    conn.execute(
                        f"UPDATE carnet_items SET carnet_id=?, target_carnet_id=? "
                        f"WHERE id IN ({placeholders})",
                        tuple([new_cid, new_cid] + selected_ids)
                    )
            conn.commit()
        except Exception as e:
            conn.rollback()
            log.error("transformation echec: %s\n%s", e, traceback.format_exc())
            flash("Erreur lors de la transformation : " + str(e), "err")
            return redirect(url_for('carnet_transformer', cid_carnet=cid_carnet))
        finally:
            conn.close()
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
    _get_carnet_or_404(cid_carnet)
    if not csrf_check():
        flash("Session expiree.", "err")
        return redirect(url_for('carnet_view', cid_carnet=cid_carnet))
    execute(
        "UPDATE carnets SET deleted_at=CURRENT_TIMESTAMP WHERE id=?",
        (cid_carnet,)
    )
    return redirect(url_for('home'))


# ══════════════════════════════════════════════════════════════════════
#                    v1.5 — APERCU LIVRE + EXPORT PDF
# ══════════════════════════════════════════════════════════════════════

PDF_FORMATS = {
    'square_20':     ('Carre 20×20 cm',   200, 200),
    'landscape_a4':  ('A4 paysage',       297, 210),
    'portrait_a5':   ('A5 portrait',      148, 210),
}

PDF_LAYOUTS = [
    ('1', '1 photo / page'),
    ('2', '2 photos / page'),
    ('3', '3 photos / page'),
    ('4', '4 photos / page'),
]

PDF_MARGIN_POSITIONS = [
    ('right',  'Notes a droite'),
    ('left',   'Notes a gauche'),
    ('bottom', 'Notes en bas'),
    ('end',    'Notes en fin de livre'),
]


@app.route('/carnet/<int:cid_carnet>/apercu')
@couple_required
def carnet_apercu(cid_carnet):
    """Page apercu HTML paginée du livre photo."""
    c = _get_carnet_or_404(cid_carnet)
    sort_mode = c.get('sort_mode') or 'chrono'
    pages_data = _carnet_pages(cid_carnet, sort_mode=sort_mode)
    fmt = request.args.get('format', 'square_20')
    if fmt not in PDF_FORMATS:
        fmt = 'square_20'
    layout = request.args.get('layout', c.get('pdf_layout') or '1')
    if layout not in dict(PDF_LAYOUTS):
        layout = '1'
    margin_pos = request.args.get('margin', c.get('pdf_margin_position') or 'right')
    if margin_pos not in dict(PDF_MARGIN_POSITIONS):
        margin_pos = 'right'
    return render_template('apercu.html',
        carnet=c,
        main_pages=pages_data['main'],
        margin_pages=pages_data['margin'],
        format=fmt, layout=layout, margin_pos=margin_pos,
        formats=PDF_FORMATS, layouts=PDF_LAYOUTS, margin_positions=PDF_MARGIN_POSITIONS,
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
    """Genere le PDF du livre photo a la volee, avec layout et position marge."""
    c = _get_carnet_or_404(cid_carnet)
    fmt = request.args.get('format', 'square_20')
    if fmt not in PDF_FORMATS:
        fmt = 'square_20'
    layout = request.args.get('layout', c.get('pdf_layout') or '1')
    if layout not in dict(PDF_LAYOUTS):
        layout = '1'
    n_per_page = int(layout)
    margin_pos = request.args.get('margin', c.get('pdf_margin_position') or 'right')
    if margin_pos not in dict(PDF_MARGIN_POSITIONS):
        margin_pos = 'right'
    sort_mode = c.get('sort_mode') or 'chrono'
    pages_data = _carnet_pages(cid_carnet, sort_mode=sort_mode)

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

def _carnet_pages(carnet_id, sort_mode='chrono'):
    """
    Retourne les pages d'un carnet selon le sort_mode :
    - 'chrono' (default) : tri par date EXIF/ajout, position en tie-breaker
    - 'manual' : tri par position (drag & drop)
    Renvoie un dict avec deux listes : 'main' (album) et 'margin' (notes en marge).
    """
    if sort_mode == 'manual':
        order_by = "ap.position ASC, ap.id ASC"
    else:
        order_by = ("COALESCE(p.taken_at, v.taken_at, ap.created_at) ASC, "
                    "ap.position ASC, ap.id ASC")
    rows = query(f"""
        SELECT ap.*,
               p.file_path AS photo_path, p.thumb_path AS photo_thumb,
               p.width AS photo_width, p.height AS photo_height,
               p.taken_at AS photo_taken_at,
               p.gps_lat AS photo_gps_lat, p.gps_lng AS photo_gps_lng,
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
    }


def _next_page_position(carnet_id):
    r = query(
        "SELECT COALESCE(MAX(position), -1) + 1 AS next FROM album_pages WHERE carnet_id=?",
        (carnet_id,), one=True
    )
    return r['next'] if r else 0


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

    rel_file = f"{couple_id}/{fname}"
    rel_thumb = f"{couple_id}/{thumb_fname}"
    return {
        'file_path': rel_file,
        'thumb_path': rel_thumb,
        'width': w, 'height': h,
        'taken_at': taken_at,
        'gps_lat': gps_lat,
        'gps_lng': gps_lng,
    }


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


@app.route('/carnet/<int:cid_carnet>/album')
@couple_required
def carnet_album(cid_carnet):
    """Mode edition album : photos, captions, blocs texte, notes en marge."""
    c = _get_carnet_or_404(cid_carnet)
    sort_mode = c.get('sort_mode') or 'chrono'
    pages = _carnet_pages(cid_carnet, sort_mode=sort_mode)
    geo_photos = [p for p in pages['all']
                  if p.get('photo_gps_lat') is not None and p.get('photo_gps_lng') is not None]
    return render_template('album.html', carnet=c,
        main_pages=pages['main'], margin_pages=pages['margin'],
        structured=pages.get('structured', []),
        orphans=pages.get('orphans', []),
        geo_photos=geo_photos, types=CARNET_TYPES, sort_mode=sort_mode)


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
        # Source de verite : EXIF cote serveur (data) > client > rien
        # Le client envoie ses lectures exifr ; le serveur lit aussi via Pillow
        # quand l'original arrive non compresse cote client. On combine.
        ct = client_taken[idx] if idx < len(client_taken) else ''
        if ct and ct != 'null' and not data.get('taken_at'):
            data['taken_at'] = ct
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
            "taken_at, gps_lat, gps_lng, added_by) VALUES (?,?,?,?,?,?,?,?,?)",
            (c['couple_id'], data['file_path'], data['thumb_path'],
             data['width'], data['height'], data['taken_at'],
             gps_lat, gps_lng, session['uid'])
        )
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
        conn.execute("UPDATE carnets SET sort_mode='manual' WHERE id=?", (cid_carnet,))
        conn.commit()
    finally:
        conn.close()
    return jsonify({'ok': True, 'mode': 'manual'})


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
    # On supprime la page (la photo reste en BDD : pourra etre reutilisee plus tard)
    execute("DELETE FROM album_pages WHERE id=?", (page_id,))
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

    vid = execute(
        "INSERT INTO videos (couple_id, file_path, poster_path, duration_s, "
        "width, height, scan_token, added_by) VALUES (?,?,?,?,?,?,?,?)",
        (c['couple_id'], rel_video, rel_poster, duration_s, width, height,
         token, session['uid'])
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
            # Espace par defaut : 1er espace dont l'user est membre
            esps = user_espaces(existing['id'])
            if esps:
                session['espace_id'] = esps[0]['id']
                session['couple_id'] = esps[0]['id']  # rétro-compat
            else:
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
    inv = query(
        "SELECT * FROM invitations WHERE couple_id=? AND utilise=0 AND expires_at > ? "
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
    return render_template(
        'invite_share.html',
        invite_url=invite_url,
        qr=qr_svg(invite_url),
        couple=query("SELECT * FROM couples WHERE id=?", (cid,), one=True),
    )


@app.route('/invite/<token>', methods=['GET', 'POST'])
def invite_accept(token):
    """
    Landing pour rejoindre un espace via lien d'invitation.
    Multi-espaces : un user peut etre membre de plusieurs espaces, donc
    on l'AJOUTE comme membre (pas de blocage si deja dans un autre).
    """
    inv = query(
        "SELECT * FROM invitations WHERE token=? AND utilise=0 AND expires_at > ?",
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
            execute("UPDATE invitations SET utilise=1 WHERE id=?", (inv['id'],))
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
        execute("UPDATE invitations SET utilise=1 WHERE id=?", (inv['id'],))
        session['uid'] = uid
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
    return render_template('histoire.html',
        conv=conv, messages=msgs, members=[dict(m) for m in members],
        bubble_color=bubble_color, query=q,
        carnets_ref=[dict(c) for c in carnets_ref]
    )


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


def _recompute_sections(carnet_id):
    """Recalcule les album_sections pour un carnet (idempotent).
    Preserve les pages avec manual_order=1 a leur position actuelle."""
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
        ts = r.get('taken_at') or r.get('video_taken_at')
        if not ts:
            continue
        # Date locale (simplification : on prend les 10 premiers chars)
        day = str(ts)[:10]
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
    user = current_user()
    espaces = user_espaces(user['id'])
    # Stats : carnets de l'espace courant + photos uploadees par l'user
    eid = current_espace_id()
    stats = {'carnets': 0, 'photos': 0, 'reveries': 0, 'videos': 0}
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
    return render_template('profil.html', user=user, espaces=espaces, stats=stats)


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


# ── Bootstrap ─────────────────────────────────────────────────────────
init_db()

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=int(os.environ.get('PORT', 5050)))
