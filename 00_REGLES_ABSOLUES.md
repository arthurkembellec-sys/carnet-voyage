# 00 — RÈGLES ABSOLUES — Carnet_Voyage

> **À relire au début de CHAQUE session.** Ce fichier fait foi.
> Il fige les décisions produit et les règles d'architecture réutilisables par tous les écrans.
> Transposé le 2026-07-27 depuis les règles AqGK (`~/Dev/aqgk/00_REGLES_ABSOLUES.md`),
> adapté au périmètre réel de Carnet_Voyage (app couple/famille, un seul `app.py` de ~5 500 lignes).

---

## A. DÉCISIONS PRODUIT VERROUILLÉES

### D1 — Frontière de cloisonnement = l'ESPACE
- Un **espace** (table `couples`, historiquement « couple ») regroupe des membres via
  `espace_members(user_id, espace_id, role)`. Un user peut appartenir à plusieurs espaces.
- **Tout objet métier appartient à un espace**, directement ou via son carnet :
  `carnets`, `photos`, `album_pages`, `album_sections`, `videos`, `carnet_items`,
  `conversations`/`chapters`/`messages`, `activity_events`.
- L'espace courant vit dans `session['espace_id']` (avec fallback legacy `session['couple_id']`,
  cf. `current_espace_id()`). **On lit toujours l'espace courant par ce helper, jamais la session en direct.**
- **Le cloisonnement est P0 et non négociable** : voir les souvenirs d'un autre espace est rédhibitoire.
  Toute route neuve se teste depuis un compte d'un AUTRE espace → 403/404, jamais 200.

### D2 — Liens publics = jetons, testés depuis l'extérieur
- Les surfaces publiques (invitation `invitations`, page vidéo publique, partage) sont accessibles
  **sans session**, uniquement par jeton. Une surface publique ne doit **jamais** exposer autre chose
  que la ressource visée : la non-fuite se prouve avec un **jeton étranger**, pas en lecture de code.

### D3 — Mobile d'abord (verrouillé v4.8, commit `b96d991`)
- L'écran de référence est le **téléphone**. Cibles tactiles **44 px** minimum, outils visibles
  **sans survol** (il n'y a pas de hover sur mobile), modales en **feuille** basse.
- Une interaction qui n'existe qu'au survol ou au clic droit est un **bug d'accessibilité**, pas une finition.

### D4 — Rien du contenu utilisateur ne disparaît en silence
- Photos, notes, épingles, jours de trajet : un élément qui n'entre dans aucun groupe est **listé
  à part** (groupe « orphelins », affiché en bas), jamais fusionné ni masqué.
- Toute suppression réelle de contenu utilisateur est **confirmée par Arthur** (cf. §E).

---

## B. RÈGLES D'ARCHITECTURE

### R1 — Cloisonnement espace
- Toute route métier porte une garde : `@login_required`, `@couple_required` (alias `espace_required`)
  ou `@admin_required`. **Pas de garde ad hoc dupliquée** — on réutilise celles d'`app.py`.
- Toute requête sur un objet métier **filtre par l'espace courant** (directement ou par jointure
  sur le carnet). Un `SELECT` par `id` seul, sans vérification d'appartenance, est un défaut de sécurité.
- Tout INSERT renseigne l'espace (jamais NULL).

### R2 — Accès DB
- Toujours via les helpers **`query()` / `execute()`** d'`app.py`. `get_db()` seulement pour les cas
  qui ont besoin de la connexion (transaction, migration). **Jamais de `sqlite3.connect` ad hoc dans une vue.**

### R3 — Base de données / migrations
- **`schema.sql` ne se modifie JAMAIS** : il est explicitement *informatif*. La vérité vit dans `init_db()`.
- Toute évolution de schéma = migration **idempotente** dans `app.py` :
  `CREATE TABLE IF NOT EXISTS …` dans `init_db()`, ou fonction `_migrate_<sujet>(conn)` sur le modèle
  de `_migrate_album_pages_video()` / `_migrate_carnets_souhait()` (pattern : lire `PRAGMA table_info`,
  `ALTER TABLE … ADD COLUMN` seulement si absent).
- **Un seul CREATE par table.** Pas de table concurrente qui doublonne une existante.
- Migrations dans le bon ordre FK. Une migration doit pouvoir tourner **deux fois de suite** sans erreur.
- **Backfill** : une nouvelle table qui remplace une colonne doit reprendre l'existant, sinon les
  carnets déjà créés perdent leur contenu au déploiement.

### R4 — Gestion d'erreur
- **Jamais de traceback brut en prod** : `@app.errorhandler(500)` logge la trace complète (Railway)
  et rend un message doux. Ne pas contourner ce handler.
- **États vides explicites** : carnet sans photo, jour sans étape, espace sans membre → message UX,
  jamais une page blanche ni un bloc vide.
- **Tout repli est BRUYANT** : si le code retombe sur une valeur par défaut (géocodage échoué, itinéraire
  indisponible, EXIF absent), l'utilisateur le VOIT (badge/mention) et le log le dit. Un fallback muet
  est un bug qui se découvre trois semaines plus tard.

### R5 — Modification du code
- `app.py` **s'édite directement** (pas de workflow `patches/` ici, contrairement à AqGK).
- **Fonctions existantes : étendre, jamais réécrire.** Pas de refactor opportuniste non demandé.
- **Les routes sont des contrats** avec les utilisateurs (liens partagés, PWA installée, favoris) :
  ne jamais renommer/supprimer une URL existante sans arbitrage explicite.
- **Une feature par session.**
- La génération PDF vit dans **`pdf_book.py`** — jamais de code PDF dans `app.py`.

### R6 — Interface unifiée
- **Un seul** `templates/_base.html`. **Un seul** fichier de thème : `static/style.css`.
- Les partials sont préfixés `_` (`_topbar.html`, `_page_item.html`, `_margin_note.html`…) et
  se réutilisent — on n'en duplique pas une variante locale.
- **Zéro CSS inline massif** dans les templates : les tokens sont dans `:root` (cf. §D).

### R7 — Injection & échappement
- Toute donnée utilisateur rendue dans un `<script>` passe par un échappement sûr :
  `json.dumps` **n'échappe pas** `</` → `.replace('</', '<\\/')`. Un titre de carnet contenant
  `</script>` ne doit pas pouvoir casser la page.
- Jinja échappe par défaut : tout `|safe` doit être justifié en commentaire.

### R8 — Hygiène du repo
- Jamais committés : `carnet.db*`, `uploads/`, `backups/`, `maps_cache/`, `.env` (déjà dans `.gitignore` — le vérifier).
- Pas de fichier mort versionné (`app_old.py`, `app_modifie.py`…).
- Briefs exécutés → `briefs/archive/`, préfixés `AAAA-MM-JJ_`.
- `SECRET_KEY` **jamais** en dur : variable d'environnement Railway.

---

## C. CONVENTIONS TECHNIQUES

- **Session** : `uid`, `espace_id` (+ `couple_id` legacy maintenu par `set_current_espace()`), `_csrf`.
- **Auth** : bcrypt, invitation-only (pas d'auto-inscription ouverte) ; admin = email dans `ADMIN_EMAILS`.
- **Stack** : Flask 3 + SQLite partout (local `carnet.db`, prod `/app/data/carnet.db` sur volume Railway
  `carnet-data`), Jinja2 + Vanilla JS + Leaflet (vendored dans `static/vendor/`, **pas de CDN**
  pour le JS applicatif), gunicorn 2 workers, healthcheck `/healthz`.
- **Port local** : 5050 (AqGK est sur 5000, zéro conflit).
- **Versionnage** : `feat(vX.Y): …` / `fix(vX.Y.Z): …` dans les messages de commit.
- **Texte du code et messages de commit : ASCII, sans accents** (convention du dépôt, cf. `git log`),
  UNE seule ligne, sans caractère interprété par le shell (`& ( ) ' ! < >` interdits ; tirets et virgules OK).
  L'UI utilisateur, elle, est en français accentué normal.

---

## D. CONVENTIONS VISUELLES (charte « Notre Histoire »)

- **Polices** : `Fraunces` (display/titres), `Geist` (UI). Variables `--font-display` / `--font-ui`.
- **Couleurs** : crème chaud. Accent par défaut `#A8503D`, surchargeable par espace via
  `[data-space-accent]`. Fond `#FBFBFA`, encre `#17181A`.
- **Toujours passer par les tokens CSS** (`--accent`, `--space-*`, `--radius-*`, `--motion-*`) :
  aucune valeur hexadécimale ni pixel magique en dur dans un template.
- **Largeur de contenu** : `--content-max-width: 720px`.
- Règle d'or : un visuel validé quelque part devient la référence universelle. On l'étend, on ne le réinvente pas.

---

## E. QUAND NE PAS DÉCIDER SEUL (demander à Arthur)

- Toute **migration DB irréversible** ou suppression de données (locale **ou** prod).
- Tout **retrait** de code vivant (route, écran, moteur) non explicitement demandé.
- Toute **invention de données** (contenu de carnet, lieux, dates) : interdite — squelette + alerte.
- Tout changement d'**URL existante**.
- Un brief **ambigu sur un point structurant** : poser LA question avec les options, ne pas trancher en silence.

---

## F. CHECKLIST DE FIN DE SESSION

Voir `briefs/VIGILANCE_VERIFICATION_CODE.md` §3 pour la batterie détaillée. En résumé :

1. `python -c "import ast; ast.parse(open('app.py').read())"` → syntaxe OK.
2. `python -c "from app import app"` → imports + migrations OK (init_db tourne au chargement).
3. `grep "^def " app.py | sort | uniq -d` → aucun doublon de fonction.
   (⚠️ **sans `-n`** : avec le numéro de ligne, `uniq -d` ne trouve jamais rien — le check
   historique du `CLAUDE.md` était inopérant, corrigé le 2026-07-27.)
4. `grep "@app.route" app.py | sort | uniq -d` → comparé au même grep AVANT le diff.
5. Migrations rejouées deux fois de suite sans erreur.
6. Un test négatif au moins : le comportement interdit échoue vraiment.
7. **Commit + push autonomes** (règle du 2026-07-27) — après la batterie, jamais avant.
   `git add` **nommément**, jamais `-A`.
8. Brief exécuté archivé dans `briefs/archive/AAAA-MM-JJ_BRIEF_*.md`, dans le même commit.

---

## G. ⬜ À ARBITRER (non transposé depuis AqGK, en attente d'Arthur)

Ces points existent chez AqGK mais n'ont **pas** d'équivalent décidé ici — ne pas les appliquer sans validation :

- **Suite de tests** : Carnet_Voyage n'a pas de dossier `tests/`. AqGK a une batterie complète.
  Question : créer `tests/` (smoke des GET + cloisonnement espace) ?
- **`deploy.sh`** : AqGK déploie via `./deploy.sh "message"`. Ici c'est `git commit` + `git push`
  directs. Question : vaut-il le coup d'avoir un `deploy.sh` ici aussi ?
- ~~Déploiement autonome~~ : **tranché le 2026-07-27** — l'assistant commit et pousse seul,
  comme chez AqGK. Voir `briefs/VIGILANCE_VERIFICATION_CODE.md` §6.
- **Journal de bord** : AqGK tient un `JOURNAL_DE_BORD.md`. Ici il n'y en a pas.
