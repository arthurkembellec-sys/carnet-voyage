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
from functools import wraps
from datetime import datetime, timedelta

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
APP_VERSION = "1.2.2-album-exif-gps-marge"
DB_PATH = os.environ.get('DATABASE_PATH', os.path.join(os.path.dirname(__file__), 'carnet.db'))
UPLOAD_DIR = os.environ.get('UPLOAD_DIR', os.path.join(os.path.dirname(DB_PATH), 'uploads'))
SECRET_KEY = os.environ.get('SECRET_KEY') or secrets.token_urlsafe(32)
INVITATION_TTL_DAYS = 14

os.makedirs(os.path.dirname(DB_PATH) or '.', exist_ok=True)
os.makedirs(UPLOAD_DIR, exist_ok=True)

app = Flask(__name__)
app.config['SECRET_KEY'] = SECRET_KEY
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50 Mo upload
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


def init_db():
    """
    Migrations idempotentes. Toute nouvelle table / colonne s'ajoute ici,
    en respectant l'ordre (les FK dependantes apres leurs cibles).
    """
    conn = get_db()
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


def couple_required(view):
    @wraps(view)
    def wrapper(*a, **kw):
        if not session.get('uid'):
            return redirect(url_for('login', next=request.path))
        if not session.get('couple_id'):
            return redirect(url_for('onboarding_couple'))
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
    return {
        'current_user': current_user(),
        'csrf_token': csrf_token,
        'app_version': APP_VERSION,
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


@app.route('/')
def home():
    """Accueil : liste verticale des carnets du couple, avec filtre par type."""
    if not session.get('uid'):
        return redirect(url_for('login'))
    if not session.get('couple_id'):
        return redirect(url_for('onboarding_couple'))
    cid = session['couple_id']
    type_filter = request.args.get('type') or ''
    if type_filter and type_filter not in dict(CARNET_TYPES):
        type_filter = ''
    if type_filter:
        rows = query(
            "SELECT * FROM carnets WHERE couple_id=? AND type=? AND deleted_at IS NULL "
            "ORDER BY COALESCE(date_start, created_at) DESC, id DESC",
            (cid, type_filter)
        )
    else:
        rows = query(
            "SELECT * FROM carnets WHERE couple_id=? AND deleted_at IS NULL "
            "ORDER BY COALESCE(date_start, created_at) DESC, id DESC",
            (cid,)
        )
    return render_template(
        'index.html',
        carnets=[dict(r) for r in rows],
        types=CARNET_TYPES,
        type_filter=type_filter,
    )


# ── Routes : carnets ─────────────────────────────────────────────────
def _get_carnet_or_404(cid_carnet):
    """Recupere un carnet en verifiant qu'il appartient au couple courant."""
    c = query("SELECT * FROM carnets WHERE id=? AND deleted_at IS NULL", (cid_carnet,), one=True)
    if not c or c['couple_id'] != session.get('couple_id'):
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
            (session['couple_id'], data['title'], data['type'], data['location'],
             data['date_start'], data['date_end'], 'active', session['uid'])
        )
        return redirect(url_for('carnet_view', cid_carnet=cid))
    return render_template('carnet_form.html', mode='nouveau', carnet=None, types=CARNET_TYPES)


@app.route('/carnet/<int:cid_carnet>')
@couple_required
def carnet_view(cid_carnet):
    c = _get_carnet_or_404(cid_carnet)
    return render_template('carnet_view.html', carnet=c, types=CARNET_TYPES)


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
#                         v1.2 — ALBUM
# ══════════════════════════════════════════════════════════════════════

def _carnet_pages(carnet_id):
    """
    Retourne les pages d'un carnet ordonnees en TIMELINE chronologique :
    - Photos triees par date EXIF (taken_at) si dispo, sinon date d'ajout
    - Blocs texte intercales par date d'ajout (created_at)
    Renvoie un dict avec deux listes : 'main' (album) et 'margin' (notes en marge).
    """
    rows = query("""
        SELECT ap.*,
               p.file_path AS photo_path, p.thumb_path AS photo_thumb,
               p.width AS photo_width, p.height AS photo_height,
               p.taken_at AS photo_taken_at,
               p.gps_lat AS photo_gps_lat, p.gps_lng AS photo_gps_lng,
               u.display_name AS added_by_name
        FROM album_pages ap
        LEFT JOIN photos p ON p.id = ap.photo_id
        LEFT JOIN users u ON u.id = ap.added_by
        WHERE ap.carnet_id = ?
        ORDER BY
            COALESCE(p.taken_at, ap.created_at) ASC,
            ap.position ASC, ap.id ASC
    """, (carnet_id,))
    pages = [dict(r) for r in rows]
    main = [p for p in pages if not p.get('is_margin')]
    margin = [p for p in pages if p.get('is_margin')]
    return {'main': main, 'margin': margin, 'all': pages}


def _next_page_position(carnet_id):
    r = query(
        "SELECT COALESCE(MAX(position), -1) + 1 AS next FROM album_pages WHERE carnet_id=?",
        (carnet_id,), one=True
    )
    return r['next'] if r else 0


def _save_uploaded_photo(file, couple_id):
    """
    Sauvegarde une photo uploadee :
    - Decode + corrige EXIF orientation
    - Resize a 2000px max (cote long), qualite 85
    - Genere un thumbnail 400px (qualite 70)
    - Renomme en token random pour eviter collision
    Retourne dict {file_path, thumb_path, width, height, taken_at}.
    """
    img = Image.open(file.stream)

    # EXIF : orientation + date prise
    taken_at = None
    try:
        exif = img._getexif() or {}
        orient_key = next((k for k, v in ExifTags.TAGS.items() if v == 'Orientation'), None)
        if orient_key and orient_key in exif:
            o = exif[orient_key]
            if o == 3: img = img.rotate(180, expand=True)
            elif o == 6: img = img.rotate(270, expand=True)
            elif o == 8: img = img.rotate(90, expand=True)
        date_key = next((k for k, v in ExifTags.TAGS.items() if v == 'DateTimeOriginal'), None)
        if date_key and date_key in exif:
            try:
                taken_at = datetime.strptime(exif[date_key], '%Y:%m:%d %H:%M:%S').isoformat()
            except Exception:
                taken_at = None
    except Exception:
        pass

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
    }


@app.route('/carnet/<int:cid_carnet>/album')
@couple_required
def carnet_album(cid_carnet):
    """Mode edition album : photos, captions, blocs texte, notes en marge."""
    c = _get_carnet_or_404(cid_carnet)
    pages = _carnet_pages(cid_carnet)
    # Photos avec coords GPS pour la mini-carte
    geo_photos = [p for p in pages['all']
                  if p.get('photo_gps_lat') is not None and p.get('photo_gps_lng') is not None]
    return render_template('album.html', carnet=c,
        main_pages=pages['main'], margin_pages=pages['margin'],
        geo_photos=geo_photos, types=CARNET_TYPES)


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
        # Override avec metadata client si dispos (Canvas perd les EXIF)
        ct = client_taken[idx] if idx < len(client_taken) else ''
        if ct and ct != 'null':
            data['taken_at'] = ct
        gps_lat = _safe_float(client_lat[idx]) if idx < len(client_lat) else None
        gps_lng = _safe_float(client_lng[idx]) if idx < len(client_lng) else None
        is_margin = (client_margin[idx] == '1') if idx < len(client_margin) else False
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
    return jsonify({'ok': True, 'created': created, 'errors': errors})


@app.route('/album_page/<int:page_id>/margin', methods=['POST'])
@couple_required
def page_toggle_margin(page_id):
    """Bascule une page entre album principal et note marginale."""
    if not csrf_check():
        return jsonify({'ok': False, 'error': 'CSRF'}), 403
    page = query("SELECT ap.*, c.couple_id FROM album_pages ap "
                 "JOIN carnets c ON c.id=ap.carnet_id WHERE ap.id=?",
                 (page_id,), one=True)
    if not page or page['couple_id'] != session['couple_id']:
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
    if not page or page['couple_id'] != session['couple_id']:
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
    if not page or page['couple_id'] != session['couple_id']:
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
    if not page or page['couple_id'] != session['couple_id']:
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
    if owner_couple != session.get('couple_id'):
        abort(403)
    return send_from_directory(UPLOAD_DIR, filename, max_age=31536000)


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
            session['couple_id'] = None
            return redirect(next_url if next_url.startswith('/') else '/')
        else:  # login
            if not existing or not check_pw(password, existing['password_hash']):
                flash("Email ou mot de passe incorrect.", "err")
                return render_template('login.html', email=email, next_url=next_url)
            session['uid'] = existing['id']
            session['couple_id'] = existing['couple_id']
            return redirect(next_url if next_url.startswith('/') else '/')
    return render_template('login.html', next_url=next_url)


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


# ── Routes : onboarding couple ────────────────────────────────────────
@app.route('/onboarding/couple', methods=['GET', 'POST'])
@login_required
def onboarding_couple():
    """Creation du couple par le 1er user. Redirige si deja dans un couple."""
    user = current_user()
    if user.get('couple_id'):
        return redirect(url_for('home'))
    if request.method == 'POST':
        if not csrf_check():
            flash("Session expiree.", "err")
            return redirect(url_for('onboarding_couple'))
        name = (request.form.get('name') or '').strip()
        cid = execute(
            "INSERT INTO couples (name, created_by) VALUES (?,?)",
            (name, user['id'])
        )
        execute("UPDATE users SET couple_id=? WHERE id=?", (cid, user['id']))
        session['couple_id'] = cid
        return redirect(url_for('invite_share'))
    return render_template('onboarding.html', user=user)


@app.route('/invite/share')
@couple_required
def invite_share():
    """Genere (si besoin) un lien d'invitation actif et affiche QR + URL."""
    cid = session['couple_id']
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
    """Landing 2e partenaire : signup + auto-rattachement au couple."""
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

    # Si deja connecte avec un autre couple → bloque
    user = current_user()
    if user and user.get('couple_id') and user['couple_id'] != inv['couple_id']:
        flash("Vous etes deja dans un autre couple.", "err")
        return redirect(url_for('home'))

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
        # Si user existant non rattache → on rattache, sinon nouveau user
        existing = query("SELECT * FROM users WHERE email=?", (email,), one=True)
        if existing:
            if not check_pw(password, existing['password_hash']):
                flash("Cet email existe deja. Le mot de passe ne correspond pas.", "err")
                return render_template('invite_accept.html', couple=couple, token=token, email=email)
            if existing['couple_id'] and existing['couple_id'] != inv['couple_id']:
                flash("Cet email est deja rattache a un autre couple.", "err")
                return render_template('invite_accept.html', couple=couple, token=token)
            execute("UPDATE users SET couple_id=? WHERE id=?", (inv['couple_id'], existing['id']))
            uid = existing['id']
        else:
            uid = execute(
                "INSERT INTO users (email, display_name, password_hash, couple_id) VALUES (?,?,?,?)",
                (email, display_name or email.split('@')[0], hash_pw(password), inv['couple_id'])
            )
        execute("UPDATE invitations SET utilise=1 WHERE id=?", (inv['id'],))
        session['uid'] = uid
        session['couple_id'] = inv['couple_id']
        return redirect(url_for('home'))

    return render_template('invite_accept.html', couple=couple, token=token)


# ── Bootstrap ─────────────────────────────────────────────────────────
init_db()

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=int(os.environ.get('PORT', 5050)))
