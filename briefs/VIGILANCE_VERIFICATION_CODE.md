# VIGILANCE & VÉRIFICATION — Carnet_Voyage

> Transposé le 2026-07-27 depuis `~/Dev/aqgk/briefs/VIGILANCE_VERIFICATION_CODE.md`,
> adapté au dépôt Carnet_Voyage.
> Le principe unique : **ne vérifie pas que ton code marche — essaie de le CASSER,
> et vérifie que le mauvais comportement est IMPOSSIBLE.**
> Un test qui confirme ce que tu viens d'écrire ne prouve rien ; un test qui échoue
> si quelqu'un réintroduit le bug, si.

## 1. L'état d'esprit

- **Teste l'AFFIRMATION, pas le code.** « L'épingle appartient aux jours 1 et 2 » se teste en
  relisant la BASE après sauvegarde, pas l'écran. « Idempotent » se teste en appelant **deux fois**.
  « Non-fuite » se teste depuis un compte d'un **autre espace** (ou un **jeton étranger** pour une
  page publique). « Réversible » se teste aller-**retour** avec comparaison d'état complet.
- **Cherche l'effet de second ordre.** Un fix local change souvent un comportement global :
  qui d'autre consomme ce que je viens de changer ? Une colonne lue par la carte l'est peut-être
  aussi par l'album, le PDF et la vue rêverie.
- **Sonde les données RÉELLES avant d'écrire un template ou un mapping** : structure exacte du
  payload, contenu réel de `carnet.db` — jamais la forme supposée. Un champ absent en local peut
  exister en prod (et l'inverse) : le dire.
- **La vérité = le modèle RÉSOLU, jamais le déclaré.** `schema.sql` est informatif et en retard :
  la structure vraie se lit avec `PRAGMA table_info(<table>)` sur `carnet.db`, ou dans `init_db()`.
- **Écris le résultat attendu AVANT de lancer le test.** Un écart inexpliqué = bug, même si
  « ça a l'air de marcher ».

## 1-bis. LE PARCOURS D'ERREUR FAIT PARTIE DE LA RECETTE

Tout lot avec recette utilisateur teste aussi le **mauvais geste**, pas seulement le bon.
Pour chaque écran/geste livré :

- l'utilisateur fait l'erreur **plausible** (mauvaise épingle glissée, photo déposée au mauvais
  endroit, doublon créé, champ laissé vide, retour arrière en plein upload) → que **voit-il** ?
  Le message dit QUOI et POURQUOI, jamais un silence ni un jargon ;
- et surtout : comment il **répare seul**, sans notice — annulation, bouton de correction évident,
  ou refaire par-dessus. **Jamais de cul-de-sac** : une erreur dont on ne sort pas seul est un bug,
  même si le chemin nominal est parfait.
- **Mobile d'abord** (D3) : le geste se teste au **doigt**, en largeur téléphone. Pas de hover.
- **Les deux orientations** (D3 étendu v5.16) : chaque outil livré se vérifie en **portrait
  (390 px) ET en paysage** — capture d'écran des deux, la croix/le bouton doit se voir.
- **Résultat immédiat** (D3 étendu v5.16) : après le geste, l'effet est à l'écran sans
  rechargement ; un `location.reload()` comme seul retour visuel est un défaut de recette.

Le rapport de lot liste les erreurs testées et leur chemin de réparation.

## 2. Les pièges récurrents de CE dépôt

| Piège | Règle |
|---|---|
| Repli silencieux (géocodage, OSRM, EXIF, cache carte) | tout fallback est BRUYANT : mention à l'écran + log. « Loggé » seul ne suffit pas |
| Injection dans `<script>` | `json.dumps` n'échappe PAS `</` → `.replace('</', '<\\/')`. Titres et notes de carnet sont du texte utilisateur |
| Gros littéraux JS générés côté Jinja (`GEO_ITEMS` dans `carnet_souhait.html`) | ils sont **construits en Python puis injectés** : un changement de forme casse le JS SANS erreur Python. Vérifier la page dans le navigateur, pas seulement `py_compile` |
| Appels réseau externes dans un test (OSRM, Nominatim, tuiles) | les **intercepter/moquer** : un test qui dépend du réseau ment un jour sur deux, et on ne martèle pas un service public |
| `schema.sql` en retard | migrations = `init_db()` + `_migrate_*()` **seulement** ; `schema.sql` reste informatif |
| Migration non idempotente | `ALTER TABLE … ADD COLUMN` échoue au 2e passage : toujours lire `PRAGMA table_info` d'abord, ou try/except ciblé. Rejouer la migration deux fois |
| Migration sans backfill | une nouvelle table qui remplace une colonne doit **reprendre l'existant**, sinon les carnets déjà créés se vident au déploiement |
| SQLite écrit à travers un mount / verrou git | ne pas écrire sur une DB montée ni forcer un `.git/index.lock` : copier vers le scratchpad, et purger `__pycache__` après une copie de fichiers |
| Insertion automatisée muette (`sed`, script de patch) | toute insertion se **vérifie** (grep/assert « inséré: True ») ; préférer une édition explicite |
| Faux doublons de routes | les paires GET/POST préexistantes ne sont pas des doublons : comparer le grep AVANT et APRÈS le diff |
| 500 en prod | **jamais** de diagnostic par redéploiements successifs : reproduire en local (cf. §5) |
| Cloisonnement | CHAQUE route neuve : test depuis un autre espace → 403/404 ; page publique : non-fuite testée avec un jeton étranger |
| Contenu qui disparaît | rien ne se perd en silence (D4) : orphelins listés à part, jamais fusionnés ni masqués |
| URL modifiée | les routes sont des contrats (PWA installée, liens partagés) : on n'en renomme aucune sans arbitrage |

## 3. Batterie OBLIGATOIRE en fin de lot (dans cet ordre)

```bash
cd ~/Dev/Carnet_Voyage
python -c "import ast; ast.parse(open('app.py').read())"   # 1. syntaxe
python -c "import ast; ast.parse(open('pdf_book.py').read())"
python -c "from app import app"                            # 2. imports + init_db()
grep "^def " app.py | sort | uniq -d                       # 3. pas de fonction dupliquee
                                                           #    (SANS -n : le numero de ligne
                                                           #     rend uniq -d inoperant)
grep "@app.route" app.py | sort | uniq -d                  # 4. pas de route dupliquee
```

5. **Migrations rejouées** : relancer `from app import app` une 2e fois → aucune erreur
   (preuve d'idempotence).
6. **Templates touchés** : rendre la page réellement (cf. §5) — `py_compile` ne voit rien
   d'une accolade Jinja cassée ni d'un littéral JS mal formé.
7. **Tests du lot** : au moins **un test négatif** (le comportement interdit échoue) et,
   si le lot touche un format persisté, un test d'invariant sur la donnée relue en base.
8. **Smoke** : les GET principaux, 0×500 — noter le NOMBRE de routes (il doit monter du nombre
   exact de routes ajoutées).
9. **Cloisonnement** si les données changent.
10. Si un test/comportement EXISTANT casse : c'est peut-être un invariant qui change
    légitimement — l'adapter **en le disant** dans le rapport, jamais le supprimer en douce.

## 4. Rapport de fin de lot

- **Chiffres** : N vérifications passées, smoke N GET 0×500, migrations rejouées OK.
- **Les LIMITES dites honnêtement** (« non testé en réel », « champ supposé absent en prod ») —
  une limite tue = un incident plus tard.
- **Ce qui a été DÉCIDÉ en route et pourquoi** (une ligne par décision).
- **Message de commit** : UNE ligne, ASCII sans accents, sans `& ( ) ' ! < >`,
  préfixe `feat(vX.Y):` / `fix(vX.Y.Z):`.
- Brief annoté puis archivé dans `briefs/archive/AAAA-MM-JJ_BRIEF_*.md`.

## 5. Debug d'un 500 / d'une page cassée EN LOCAL, sans déployer

La page d'erreur de prod masque le traceback (R4) → ne jamais diagnostiquer un 500 à l'aveugle
par déploiements successifs. Reproduire en local sur `carnet.db` :

```bash
cd ~/Dev/Carnet_Voyage && source .venv/bin/activate
python - <<'PY'
import app as A, traceback
c = A.app.test_client()
with A.app.test_request_context('/'):
    pass
# Vue directe avec session simulee :
with A.app.test_client() as c:
    with c.session_transaction() as s:
        s['uid'] = 1; s['espace_id'] = 1
    r = c.get('/carnet/1')          # route a diagnostiquer
    print(r.status_code, len(r.data))
PY
```

En cas de 500, le traceback complet part dans le log (`@app.errorhandler(500)` fait
`traceback.format_exc()`) : le lire, corriger, **prouver**, seulement ensuite pousser.

## 6. Déploiement AUTONOME (règle Arthur du 2026-07-27)

**Commit et push se font seuls, sans demander.** Le sas humain a disparu — ces règles
le remplacent, elles ne sont pas optionnelles :

- **Un deploy n'est JAMAIS un test.** La batterie (§3) passe AVANT le push, pas après.
  Un doute = pas de push.
- `git status` **avant** `git add` : la liste des fichiers embarqués se LIT. Jamais de
  `git add -A` aveugle (la DB, les uploads et les backups doivent rester dehors) —
  on ajoute les fichiers **nommément**.
- Le rapport de lot (§4) est rendu **avec** le commit, pas à la place des vérifications.
- Pouvoir déployer n'est **pas** pouvoir décider : tout le §7 reste interdit sans Arthur.
- Push sur `main` → Railway (`confident-gratitude` / service `web`) auto-déploie.
  Vérifier ensuite que `https://histoire.aqgk.fr/healthz` répond et que les routes touchées vivent.
- **Un deploy cassé se répare par un fix AVANT, jamais par des redéploiements exploratoires.**
  Si l'app ne boote pas (502) : reproduire le boot en local immédiatement — c'est presque
  toujours une migration ou un import.

## 7. Quand NE PAS décider seul

Voir `00_REGLES_ABSOLUES.md` §E : migration irréversible ou suppression de données, retrait de
code vivant, invention de contenu, changement d'URL existante, brief ambigu sur un point structurant.
