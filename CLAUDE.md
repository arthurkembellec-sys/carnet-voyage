# CLAUDE.md — Carnet_Voyage

## 🚨 Avant TOUTE modification, lire ces deux fichiers :

1. `00_REGLES_ABSOLUES.md` — décisions produit verrouillées + règles d'architecture non négociables
2. `briefs/VIGILANCE_VERIFICATION_CODE.md` — pièges vécus du dépôt, batterie de fin de lot
   OBLIGATOIRE, quand ne pas décider seul

**Tu confirmes avoir lu ces deux fichiers avant de proposer le moindre changement de code.**

## Mission

Carnet_Voyage est une application web de carnets de voyage partagés en couple/famille.
Stack : Flask + SQLite + Railway. Hébergé sur https://histoire.aqgk.fr.

## Workflow

Quand l'utilisateur te demande d'exécuter un brief :
1. Lis `00_REGLES_ABSOLUES.md` puis `briefs/VIGILANCE_VERIFICATION_CODE.md`
2. Lis le brief demandé (dans briefs/)
3. Pose tes questions de clarification AVANT de modifier le code
4. Applique les changements — **une seule feature par session**
5. Lance la batterie complète de `VIGILANCE_VERIFICATION_CODE.md` §3
   (syntaxe, imports, doublons de fonctions ET de routes, migrations rejouées,
   au moins un test négatif, smoke des GET)
6. Rends le rapport de lot (§4) : chiffres, limites honnêtes, décisions prises
7. **Commit et push sans rien demander** (règle Arthur du 2026-07-27) — la batterie
   du §5 passe AVANT le push, jamais après ; un doute = pas de push
8. Déplace le brief vers briefs/archive/ avec préfixe date ISO :
   `briefs/archive/AAAA-MM-JJ_BRIEF_*.md` (dans le même commit)

## Règles de modification du code

- `app.py` peut être édité directement (pas de workflow patches/ ici, contrairement à AqGK)
- Ne JAMAIS modifier `schema.sql` directement ; pour ajouter une colonne ou une table,
  ajoute une fonction de migration dans `app.py` (style `CREATE TABLE IF NOT EXISTS` ou
  `ALTER TABLE ... ADD COLUMN`, en suivant le pattern v1.x existant :
  voir `v1.4 migration carnets : ajout 'souhait' + parent_souhait_id`)
- `pdf_book.py` est le module dédié à la génération PDF.
  Modifications PDF → là-bas, pas dans `app.py`.

## Conventions

- Versionning sémantique dans les commit messages : `feat(v3.4): ...`, `fix(v3.4.1): ...`
- Briefs préfixés `BRIEF_*.md` à la racine de `briefs/` quand actifs, archivés ensuite
- Branche unique : `main` (dev solo)

## Environnement local

- DB locale : `carnet.db` à la racine (auto-créée au premier `python app.py` si absente)
- Uploads : `uploads/` à la racine
- Port : 5050 (différent d'AqGK qui est sur 5000, zéro conflit possible)
- Venv : `.venv` à la racine, activé via `source .venv/bin/activate` ou l'alias `carnet`

## Environnement prod (Railway)

- Projet Railway : `confident-gratitude`
- Service : `web`
- Volume persistant : `carnet-data` monté sur `/app/data`
- DB prod : `/app/data/carnet.db` (SQLite sur volume)
- Uploads prod : `/app/data/uploads/`
- Domaine public : `histoire.aqgk.fr`

## Backup prod

Pour faire un backup complet (DB + uploads + backups internes) :

```bash
cd ~/Dev/Carnet_Voyage
railway ssh "tar czf - -C /app/data carnet.db uploads backups" > backups/prod_$(date +%Y%m%d_%H%M).tar.gz
```

Puis copier vers iCloud Drive :

```bash
cp backups/prod_*.tar.gz ~/Library/Mobile\ Documents/com~apple~CloudDocs/
```

## Briefs

Les briefs exécutables sont à la racine de `briefs/`.
Les briefs déjà exécutés sont déplacés vers `briefs/archive/` avec un préfixe date ISO.

## Vérifications bash obligatoires avant push

Batterie minimale (la version complète, avec l'ordre et les tests négatifs, est dans
`briefs/VIGILANCE_VERIFICATION_CODE.md` §3) :

```bash
python -c "import ast; ast.parse(open('app.py').read())"  # syntaxe valide
python -c "from app import app"                            # imports + init_db OK
python -c "from app import app"                            # 2e passage : migrations idempotentes
grep "^def " app.py | sort | uniq -d                       # pas de doublons de fonctions

grep "@app.route" app.py | sort | uniq -d                  # pas de doublons de routes
```

Un template touché ne se vérifie PAS par `py_compile` : rendre la page réellement
(client de test Flask, cf. `VIGILANCE_VERIFICATION_CODE.md` §5).

## Ne jamais

- Modifier la DB locale ou prod par script automatique sans demander avant
- Committer la DB (`carnet.db`), les uploads (`uploads/`), ou les backups (`backups/`)
  — déjà dans `.gitignore`, mais à vérifier
- Pousser avec un `SECRET_KEY` hardcodé (utiliser env var Railway)
- Casser le format des routes existantes (les URLs sont des contrats avec les utilisateurs)
- Déployer pour tester : la batterie passe AVANT le push, jamais après
- Diagnostiquer un 500 par redéploiements successifs : reproduction locale obligatoire
- Livrer un repli silencieux (géocodage, itinéraire, EXIF) : tout fallback se voit à l'écran
- Réécrire une fonction existante pour la « nettoyer » : on étend, on ne réécrit pas