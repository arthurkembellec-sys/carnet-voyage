"""
Notre Histoire — app couple, web mobile-first.
Application autonome, totalement isolee d'AqGK.

Etape 0 : squelette Flask qui repond / et /healthz.
Patches futurs : v1_0_couple, v1_1_carnets, v1_2_album, v1_3_pdf, v1_4_profil.
"""
import os
import sqlite3
import secrets
from flask import Flask, render_template, jsonify, g

# ── Config ────────────────────────────────────────────────────────────
APP_VERSION = "0.1.0-bootstrap"
DB_PATH = os.environ.get('DATABASE_PATH', os.path.join(os.path.dirname(__file__), 'carnet.db'))
UPLOAD_DIR = os.environ.get('UPLOAD_DIR', os.path.join(os.path.dirname(DB_PATH), 'uploads'))
SECRET_KEY = os.environ.get('SECRET_KEY') or secrets.token_urlsafe(32)

os.makedirs(os.path.dirname(DB_PATH) or '.', exist_ok=True)
os.makedirs(UPLOAD_DIR, exist_ok=True)

app = Flask(__name__)
app.config['SECRET_KEY'] = SECRET_KEY
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50 Mo upload

# ── DB helpers (style AqGK epure) ─────────────────────────────────────
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
        # V0 — squelette de base, aucune table metier encore.
        # Patch v1_0_couple ajoutera : users, couples, invitations.
        # Patch v1_1_carnets ajoutera : carnets.
        # Patch v1_2_album ajoutera : photos, album_pages.
        # Patch v1_3_pdf ajoutera : print_orders.
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


# ── Routes ────────────────────────────────────────────────────────────
@app.route('/healthz')
def healthz():
    """Endpoint de healthcheck Railway."""
    return jsonify({'ok': True, 'version': APP_VERSION})


@app.route('/')
def home():
    """Landing page provisoire (Etape 0)."""
    return render_template('hello.html', version=APP_VERSION)


# ── Bootstrap ─────────────────────────────────────────────────────────
init_db()

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=int(os.environ.get('PORT', 5050)))
