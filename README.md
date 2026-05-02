# Notre Histoire — Carnet de voyage

App couple, web mobile-first. Application autonome, totalement isolee de l'app retail AqGK
(meme nom de domaine racine `aqgk.fr`, sous-domaine dedie `histoire.aqgk.fr`).

## Stack

- Flask 3 + Python 3.12
- SQLite (`carnet.db`)
- Jinja2 + Fraunces + Geist
- bcrypt (auth)
- qrcode (partage invitation)
- reportlab + Pillow (export PDF livre photo)
- Deploiement Railway (service distinct d'AqGK)

## Demarrage local

```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

Acces : http://localhost:5050

## Deploiement Railway

1. Push sur le repo `carnet-voyage` → auto-deploy.
2. Variable `DATABASE_PATH=/app/data/carnet.db` (volume persistant Railway).
3. Variable `SECRET_KEY` (token urlsafe 32+ caracteres).
4. CNAME OVH `histoire.aqgk.fr` → `<service>.up.railway.app`.

## Roadmap V1

| Patch | Feature |
|---|---|
| v1.0 | Couple : auth + onboarding + invitation |
| v1.1 | Carnets : CRUD fiches |
| v1.2 | Album : upload photos + captions |
| v1.3 | Apercu livre + export PDF |
| v1.4 | Profil minimal |

Voir `docs/v1-plan.md` (dans le repo AqGK) pour le detail.

## Structure

```
app.py              Application Flask, routes, helpers DB
schema.sql          DDL initial (informatif — la verite est dans init_db())
templates/          Jinja templates (charte produit)
static/             CSS + JS + uploads photos
docs/decisions/     Notes des choix non-evidents
```

## Bonnes pratiques

- Aucune modification d'AqGK.
- Migrations idempotentes dans `init_db()` (liste `migrations = [...]`).
- Toute nouvelle feature passe par un patch isole (ex : `patches/v1_0_couple.py`)
  une fois `merge_patches.py` introduit (V2).
- Tests manuels sur telephone reel apres chaque deploiement.
