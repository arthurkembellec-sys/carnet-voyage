# AUDIT DE DIRECTION ARTISTIQUE — Moteur de livre PDF « Notre Histoire »

**Auditeur** : DA print, spécialité beaux livres photo
**Périmètre** : `/tmp/cv/pdf_book.py` (1360 l.), `/tmp/cv/app.py` (circuits carnet_pdf, cartes, pages), base de test `/tmp/cv/carnet.db`, PDF témoin `/tmp/livre_test.pdf` (rasterisé et examiné).
**Cadre** : le livre imprimé est l'objectif final du produit. Tout ce qui se perd entre l'app et le papier est une promesse non tenue.

---

## 1. LE LIVRE COMME OBJET — critique du chemin de fer

### Chemin de fer actuel (pdf_book.py:829-887)

```
Couverture → page blanche (dos) → carte d'ensemble → page blanche
→ pages composites (bandeau de jour à 8,5 pt) → [notes en marge fin de livre]
→ colophon « Fin »
```

### Constats, pièce en main (rasterisation de /tmp/livre_test.pdf)

| Constat | Localisation | Verdict DA |
|---|---|---|
| La couverture flotte : photo posée à 40 % de hauteur, titre à 30 %, un trou de ~6 cm entre les deux | pdf_book.py:915-917 et 942 | Composition non maîtrisée. Une couverture de beau livre est soit pleine page (photo au fond perdu + titre en réserve), soit très construite. |
| Dates de couverture en ISO brut : « 2026-07-11 → 2026-07-13 » | pdf_book.py:949-951 | Rédhibitoire sur un objet offert. On attend « 11 – 13 juillet 2026 ». |
| Page 2 = blanche, page 4 = blanche | pdf_book.py:841, 846 | Deux pages blanches dans les 4 premières = sensation d'objet vide. La tradition éditoriale y met page de garde, faux-titre, page de titre. |
| La carte d'ensemble du PDF témoin est **une page quasi vide** : titre « Notre voyage », « 1 lieu(x) sur la carte », © OSM — aucune carte | pdf_book.py:1000 (`if png:` → si le fetch échoue, rien n'est dessiné ; le message « Carte indisponible » ne sort que sur exception de dessin, 1012-1016) | Une page de titre sans contenu part à l'impression sans garde-fou. |
| Ouverture de journée = un bandeau texte de 8,5 pt + filet (day_label) | pdf_book.py:1066-1073 | C'est une rubrique, pas une ouverture de chapitre. Le rythme d'un livre de voyage se construit sur des respirations pleine page. |
| Colophon = page presque vide avec « Fin » | pdf_book.py:1248-1261 | Occasion manquée : ni statistiques du voyage, ni mention d'impression, ni date de fabrication. |
| Pas de 4e de couverture | programme 829-887 | Le PDF se termine sur le colophon : aucun service d'impression ne peut en faire une couverture arrière. |
| Fond perdu conditionnel : bleed activé **seulement** si une photo est en mode full/spread | pdf_book.py:268-269 | Deux carnets du même format n'ont pas les mêmes dimensions de document. Pour Blurb/CEWE, le gabarit doit être constant : bleed toujours présent. |
| Mélange typographique Times-Italic / Helvetica sans hiérarchie posée | tout le fichier | Passable, mais un livre se tient sur 2 familles et 4 corps maximum, définis une fois (constantes), pas au fil des appels `setFont`. |

### Ce qu'on attend d'un beau livre de voyage (et qui manque)

1. **Page de garde + page de titre** (remplace les 2 blanches) : garde teintée crème/terracotta ; page de titre = titre, prénoms du couple, dates en toutes lettres.
2. **Ouverture de chapitre par jour** : pleine page (ou belle demi-page) avec « JOUR 3 », date en toutes lettres, lieu dominant, éventuellement la meilleure photo du jour et 1 ligne de résumé. Le `_program_chunks` (pdf_book.py:137-172) coupe déjà aux changements de jour : la structure existe, il ne manque que la page.
3. **Double page carte** en ouverture (voir §4) au lieu de la page simple actuelle (kind `overview_map` force recto, verso blanc gâché — 1268-1272).
4. **Page de statistiques du voyage** (avant le colophon) : X jours, Y photos, Z vidéos, N lieux, ~km parcourus (haversine sur les GPS chronologiques — les données sont dans `photos.gps_lat/lng`), villes traversées. C'est LA page que les couples montrent.
5. **4e de couverture** : photo secondaire ou carte miniature + baseline « Notre Histoire ».
6. **Pagination cohérente** : garder les pages blanches non numérotées, mais faire démarrer la page 1 sur la première page de contenu **droite** (convention : les belles pages sont impaires ; aujourd'hui le compteur démarre au petit bonheur, pdf_book.py:1264-1265).

---

## 2. LES COMMENTAIRES — audit du circuit complet vers le papier

Trois types de commentaires existent dans l'app. Bilan : **un type est totalement perdu, les deux autres fuient.**

### 2.1 Épingles `photo_notes` (v5.8) — PERDUES À 100 % ✗ CONFIRMÉ

- Schéma : app.py:679-688 (`photo_notes(photo_id, x, y, texte, auteur_id)` — x,y normalisés 0..1, cf. données de test : `x=0.7, y=0.3, « Note de Laurie »` sur la photo 240).
- Création/suppression : app.py:4676-4679 / 4723-4731.
- Chargement **uniquement** pour la vue album web : app.py:4119-4131 (`photo_notes.setdefault(...)`, passé au template ligne 4165).
- **Le mot `photo_notes` n'apparaît nulle part dans pdf_book.py** (vérifié sur les 1360 lignes) et l'appel `render_carnet_pdf` (app.py:2636-2653) ne transmet aucun paramètre de notes. `_carnet_pages` (app.py:3315-3346) ne fait pas non plus la jointure.
- → Le retour client « les commentaires n'apparaissent pas dans le rendu » est **exact et structurel** : la donnée n'entre jamais dans le moteur.

**Spécification — convention du métier pour les épingles x,y à l'impression.**
Dans l'édition photo, on n'imprime jamais de bulle par-dessus l'image. La convention est **l'appel de note** :

1. Sur la photo : pastille discrète ①②③ (cercle 3,2 mm, fond crème alpha 0,85 — même recette que le cartouche des lettres, pdf_book.py:427-430 —, chiffre encre 7 pt) posée à `(px, py) = (cx + x·dw, cy + (1−y)·dh)` (attention à l'origine : x,y app sont top-left, ReportLab est bottom-left). Clamper la pastille à 2 mm du bord de l'image.
2. Le texte part **dans la zone marge de la même page composite**, sous les légendes, dans un bloc « ① LAURIE — “On y retourne ?” » : numéro terracotta, auteur en petites capitales 6,5 pt (la donnée `auteur` est déjà jointe, app.py:4125), texte en Times-Italic 8,5 — c'est le charme du produit couple : *qui* a écrit quoi.
3. Sur photo pleine page/spread : reporter les notes dans le bandeau de légende (full) ou sur la page en regard (spread).
4. Garde-fou : > 4 notes sur une photo → regrouper « ①–⑤ » et lister en marge.

**Implémentation** : jointure dans `_carnet_pages` (`LEFT JOIN photo_notes` agrégées par photo_id, ou requête séparée injectée `p['notes'] = [...]`), nouveau paramètre `photo_notes` de `render_carnet_pdf`, rendu dans `_draw_image_box` (le point d'ancrage `(cx, cy, dw, dh)` est déjà retourné ligne 436) et ajout des entrées `kind:'photo_note'` dans `margin_entries` (pdf_book.py:1160-1180).

### 2.2 Légendes (`album_pages.caption`) — cinq fuites identifiées

Le circuit nominal : caption inline sous la photo (pdf_book.py:432-435) **ou**, si la page a une zone marge, en marge avec lettres a/b/c (1136-1144 puis 1160-1171). Les fuites :

1. **Légende perdue en cas de marge pleine** : `_draw_margin_zone` rend `non_dessines` (766-768, 796-798), mais le report ne garde que `kind != 'caption'` (pdf_book.py:1210-1211). **Une légende qui ne tient pas dans la case marge disparaît du livre, silencieusement.** Le commentaire du code assume ce choix (« une légende suit sa photo ») mais la conséquence réelle est la perte. Correctif : si des captions sont dans `reste`, re-dessiner ces captions **inline** sous leur photo (on connaît les boxes) ou agrandir la case marge au calepinage suivant ; a minima les reporter en fin de livre avec leur vignette.
2. **Spread : légende jamais dessinée.** `_draw_spread_half` (1029-1054) ignore `item['caption']` — contrairement au mode full qui a son bandeau (461-472). Une photo promue en double page perd son texte. Correctif : bandeau de légende sur la moitié recto, même recette que 463-472.
3. **Vidéo : légende tronquée à 1 ligne** (`max_lines=1`, pdf_book.py:536-537) contre 2 pour les photos.
4. **Troncature silencieuse générale** : `_wrap_text` coupe à `max_lines` sans ellipse ni signal (ligne 362 : `lines = lines[:max_lines]`). En marge, plafond 6 lignes (ligne 817). Un utilisateur qui écrit 8 lignes en perd 2 sans le savoir. Correctif : ellipse « … » + remontée d'un avertissement dans l'aperçu (« 3 légendes seront raccourcies »).
5. **Cap inline à 2 lignes** (`cap_lines=2` par défaut, 398-399) : même en pleine page composite avec de la place dessous.

**Où doit vivre la légende** : sous la photo (inline) quand la page a ≤ 2 photos ; en marge avec lettres quand la page est dense — la logique actuelle est la bonne, c'est l'étanchéité qui fait défaut.

### 2.3 Notes de marge (`album_pages.is_margin=1`) — circuit robuste, trois trous

Le circuit (build_margin_plan pdf_book.py:175-228 → distribution 889-899 → `_draw_margin_zone` → report `marge_reportee` 1056-1057, 1084-1086 → filet de sécurité au colophon 1339-1349) est le plus abouti du moteur : rien ne tombe à la trappe pour les notes. Restent :

1. **`caption` OU `text_content`, jamais les deux** (pdf_book.py:1177) : une note de marge photo qui a une légende ET un texte perd le second.
2. **Vidéo en marge invisible en mode `end`** : `_draw_margin_grid` (1232-1246) ne traite que `photo_path` et `text_content` ; une vidéo `is_margin=1` (possible, app.py:4995-4996) donne une case vide. Idem dans la zone marge inline : `thumb_path = m.get('photo_thumb')` (1178) ignore `video_poster`.
3. **`margin_pos` réglé mais non éprouvé en `bottom`** : la bande 22 % (1103-1109) n'est pas couplée au calepinage réel — les légendes+notes+mini-carte s'y entassent avec le même plafond de 6 lignes.

---

## 3. LA CHRONOLOGIE — piste `sort_mode='manual'` : **CONFIRMÉE**

**Mécanisme (fichier + ligne) :**

- `_carnet_pages` trie par `position` et ne re-trie chrono que si `sort_mode != 'manual'` (app.py:3323, 3348-3349).
- L'ancienne route drag & drop **fige** `sort_mode='manual'` (app.py:4384 : `UPDATE carnets SET sort_mode='manual'`).
- Depuis v5.13, déplacer = re-dater (`page_deplacer`, app.py:4391-4460 : met à jour `photos.taken_at`, source `'manuel'`) — mais **ne remet pas `sort_mode` à `'chrono'`**. Sur un carnet resté en `manual` à cause d'un vieux drag, les re-datations v5.13 sont donc **sans aucun effet sur l'ordre du livre** : c'est la position figée qui gouverne.

**Preuve sur la base de test** : carnet 1 « Week end en Auvergne » est en `sort_mode='manual'`. À la position 0 trône la page id 1 datée **2026-05-04 09:30, source `manuel`** (le dernier jour du séjour imprimé en premier), et le fil des positions « remonte le temps » deux fois (2026-05-04 → 05-01 → 05-03). Trois photos re-datées à la main (`taken_at_source='manuel'`) sont prisonnières de leurs anciennes positions. C'est exactement le symptôme du client (photos re-datées qui apparaissent au mauvais endroit / en fin de bloc).

**Correctif exact (3 gestes) :**

1. Migration : `UPDATE carnets SET sort_mode='chrono' WHERE sort_mode='manual';` (la route de reset existe déjà pour l'utilisateur : app.py:4463-4471, mais il ne faut pas compter sur lui).
2. Supprimer la bascule app.py:4384 (et déprécier la route `carnet_reorder_pages` 4358-4388, remplacée par `page_deplacer`) — ou la faire déléguer à la logique de re-datation.
3. Ceinture-bretelles dans `_carnet_pages` (app.py:3348) : `if sort_mode != 'manual':` → trier chrono inconditionnellement (la clé `_page_chrono_key` 3307-3312 est saine : photo > vidéo > ajout, pages sans date en fin).

Effet de bord bénéfique : `_program_chunks` (pdf_book.py:157-172) coupe les pages au changement de jour à partir de ce même ordre — un ordre manuel incohérent fabrique aussi de **faux bandeaux de jour** dans le livre (on l'observe dans le PDF témoin : « VENDREDI 14 AOUT » surgissant après « DIMANCHE 12 JUILLET »).

---

## 4. LA CARTE — spécification d'une carte de livre digne de ce nom

### État actuel (pourquoi elle est « pas jolie et sans commentaire »)

- Tuiles **OSM standard** (`tile.openstreetmap.org`, app.py:2035) : criardes, chargées, pensées écran — alors que **l'app affiche CARTO voyager** (templates/album.html:2072, carnet_souhait.html:432). Le client compare les deux tous les jours.
- Marqueurs : simple pastille terracotta ⌀8 px + halo (app.py:2107-2110). **Aucun nom de lieu, aucun tracé, aucune distinction photo/étape, aucun numéro.**
- `_carnet_geo_summary` (app.py:2176-2220) fond photos et étapes du planning dans un même nuage de points anonymes.
- Résolution : ~4 px/mm (pdf_book.py:990-991), soit ~100 dpi — flou à l'impression (il faut 300 dpi).
- Échec silencieux → page carte vide (cf. §1).

### Spécification (Pillow, moyens du bord — `_build_map_from_tiles` sait déjà tout assembler)

**Nouvelle fonction `_build_book_map(carnet_id, w_px, h_px)` dans app.py**, dérivée de `_build_map_from_tiles` (app.py:2048-2113).

**A. Mise en page dans le livre** (pdf_book, kind `overview_map` revu) :

- Format carré 20 : zone contenu 172×174 mm. **Colonne timeline 48 mm** + gouttière 4 mm + **carte 120×174 mm**.
- Réglage utilisateur : `ALTER TABLE carnets ADD COLUMN pdf_map_timeline_side TEXT DEFAULT 'right'` (`'left'|'right'|'none'`), exposé dans les réglages d'aperçu et sauvé par `carnet_pdf_settings` (app.py:2586-2602). Côté `left` = colonne côté reliure interdit : si la page est un recto, `left` = côté gouttière → forcer côté extérieur, comme la logique outer/inner existante (pdf_book.py:258-264).
- Cible : **double page** (verso = carte pleine, recto = carte suite + timeline) en v2 ; page simple carte+timeline en v1.

**B. Résolution** : carte 120×174 mm à 300 dpi → **1417×2055 px**. Tuiles CARTO voyager en **@2x (512 px)** : `https://{a,b,c}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}@2x.png` — modifier `_fetch_osm_tile` (app.py:2018-2045) : URL paramétrable, `tile_size=512`, clé de cache préfixée `voyager2x_` (le cache actuel mélangerait les fonds). Attribution obligatoire : « © OpenStreetMap contributors © CARTO » (remplacer pdf_book.py:1020).

**C. Zoom** : `_compute_map_zoom` existant (app.py:2163-2173) sur le bbox **gonflé de 12 %** de chaque côté (respiration pour les étiquettes), calculé avec `width_px/2` (tuiles @2x), clampé 6..14.

**D. Données** : requête enrichie remplaçant `_carnet_geo_summary` —
- photos : `gps_lat/lng, thumb_path, taken_at, city_name` (jointure existante, app.py:3324-3346) ;
- étapes : `carnet_items kind='location'` avec `title, pin_kind, planned_day` (schéma app.py:569-586 ; `PIN_KINDS` avec emojis app.py:1114-1121) ;
- villes : regroupement des `photos.city_name` (déjà géocodés) pour les étiquettes.

**E. Ordre de dessin Pillow** (chaque couche = un besoin d'imprimeur) :

1. **Tuiles** voyager @2x (fond pastel déjà adapté au print).
2. **Tracé du parcours** : points (photos + étapes) triés chronologiquement, groupés par jour ; polyline `ImageDraw.line(joint='curve')`, terracotta `#C4654A`, largeur 6 px (0,5 mm), **trait plein dans la journée, pointillé entre le dernier point d'un jour et le premier du suivant** (segments 12/8 px).
3. **Badges de jour** : au premier point de chaque jour, pastille ⌀34 px encre `#1C1A17`, chiffre blanc (numéro du jour), DejaVuSans-Bold 20 px.
4. **Épingles d'étapes** : pastille ⌀40 px, couleur par `pin_kind` (dormir `#8A7AB5`, manger `#D98E4A`, rando `#6E9E75`, plage `#5FA8C4`, visite `#C4654A`, autre `#8B8378`), anneau blanc 4 px, **emoji du PIN_KINDS rendu via `ImageFont.truetype('static/vendor/fonts/NotoEmoji.ttf', 24)`** (la police est déjà embarquée pour le PDF, pdf_book.py:65-79) en blanc, centré.
5. **Épingles photo** : vignette `thumb_path` recadrée carrée puis **masque circulaire ⌀44 px + anneau blanc 4 px + ombre portée** (ellipse noire alpha 60 décalée +3/+3, sur calque RGBA composité). **Clustering** : deux épingles à < 52 px fusionnent → une vignette + badge compteur terracotta ⌀20 px en haut-droite. Plafond ~25 épingles photo (au-delà, cluster).
6. **Étiquettes de lieux** : nom de ville, DejaVuSans-Bold 26 px encre `#1C1A17`, **halo** = même texte tracé en blanc aux 8 offsets ±2 px (ou `stroke_width=3, stroke_fill='white'`). Placement glouton anti-collision : trier les villes par nombre de photos décroissant, essayer 4 positions (dessus/dessous/droite/gauche du centroïde), abandonner si toutes en collision avec un bbox déjà posé. Étiquettes d'étapes : titre de l'item, DejaVuSans 22 px, même halo.
7. **Échelle + attribution** dans un cartouche blanc alpha 200 en bas.

**F. Colonne TIMELINE** (dessinée en ReportLab à côté de l'image, pas dans le PNG — le texte reste net et sélectionnable) :

- Par jour : « **JOUR 1** — VENDREDI 1ᵉʳ MAI » (Helvetica-Bold 8,5 terracotta), dessous la ville dominante (petites capitales 7 pt encre pâle), puis les étapes du jour : pastille couleur `pin_kind` ⌀2,8 mm + numéro blanc (le même que sur la carte), titre 8 pt, « n photos » en 6,5 pt gris.
- Filet vertical LINE_RGB entre timeline et carte ; jours séparés par 4 mm.
- Source jour/étape : `planned_day` (app.py:724-736) pour un carnet planifié, sinon regroupement chronologique des photos (mêmes chunks que `_program_chunks`).
- Débord : > ~9 jours, passer la timeline sur 2 colonnes de 24 mm ou continuer au verso (double page).

**G. Robustesse** : si le PNG revient `None`, dessiner un cartouche « Carte indisponible — réessayez avec une connexion » (corriger pdf_book.py:1000-1016) **et** le signaler dans l'aperçu avant que le client n'imprime.

Les mêmes pastilles/pins servent ensuite aux **mini-cartes de section** en marge (pdf_book.py:1183-1199), aujourd'hui limitées à un point unique.

---

## 5. LE CALEPINAGE — le cas « 4 portraits en colonne » : **REPRODUIT, deux défauts distincts**

Script de test exécuté (reproduction fidèle de `_justified_boxes`, pdf_book.py:576-650, ratios réels : portrait 0,75 / paysage 1,333 / case marge 0,75) :

| Format | Contenu | Partition gagnante (code actuel) | Résultat imprimé |
|---|---|---|---|
| square_20 | 4 portraits | [2,2] | OK |
| **landscape_a4** | **4 portraits** | **SECOURS → [1,1,1,1], scale 0,13** | **4 portraits de ~34×46 mm empilés au centre d'une page de 269×184 mm** |
| **landscape_a4** | 4 portraits + case marge | **SECOURS → [1,1,1,1,1], scale 0,10** | 5 cases de 37 mm empilées |
| **landscape_a4** | 3 portraits + 1 paysage | **SECOURS → colonne, scale 0,14** | idem |
| **portrait_a5** | 4 paysages | **[1,1,1,1] gagne au score, scale 0,50** | 4 paysages 60×45 mm empilés |

### Défaut n° 1 — le filtre 70 % évalue la hauteur AVANT mise à l'échelle → fallback colonne

pdf_book.py:617 : `if len(rows) > 1 and any(rh > h * 0.72 ...)` teste la hauteur de rangée **non scalée**. En A4 paysage (zone 269×184 mm), une rangée de 2 portraits fait 177 mm > 0,72×184 = 132 mm → rejetée ; une rangée d'un portrait seul fait 359 mm → rejetée. **Toutes les partitions sont rejetées**, et le secours ligne 624 est littéralement « tout en une colonne » (`part = [1] * n`) : c'est **exactement** la photo du client. Or [2,2] scalée aurait donné deux belles rangées de 91 mm.

### Défaut n° 2 — le score mesure le remplissage en HAUTEUR, plafonné à 1

pdf_book.py:619-621 : `rempli = total*scale/h` vaut 1,0 pour **toute** composition qui déborde, pénalité forfaitaire 0,10 → toutes les compositions débordantes obtiennent 0,90, ex æquo. Comme `partitions()` (603-610) énumère `[1,1,…]` en premier et que l'égalité ne remplace pas le meilleur (`score > best[0]`, 622), **la colonne gagne tous les ex æquo** dès qu'elle passe le filtre (cas A5 + paysages). Le score ignore totalement la largeur occupée : une colonne d'images minuscules centrées « remplit » 100 % de la hauteur.

### Correctif (validé par le script sur les 3 formats × 6 scénarios : plus aucune colonne)

Dans `_justified_boxes` :

```python
scale = min(1.0, h / total) if total > 0 else 1.0
# filtre 72 % sur la hauteur APRÈS échelle
if len(rows) > 1 and any(rh * scale > h * 0.72 for _, rh in rows):
    continue
# score = COUVERTURE EN AIRE, pas en hauteur
aire = sum((rh * scale) ** 2 * ar for grp, rh in rows for ar in grp)  # bw·bh = ar·(rh·s)²
score = aire / (w * h)
score -= 0.20 * max(0.0, 1.0 - scale)          # pénalité proportionnelle à l'écrasement
if len(items) > 1 and min(rh * scale for _, rh in rows) < 30 * mm:
    score -= 0.15                               # rangée illisible à l'impression
```

**Garde-fous du métier** (à ajouter au même endroit) :

1. **Interdire la colonne** quand la zone n'est pas étroite : rejeter les partitions de plus de `ceil(n/2)` rangées dès que `w/h ≥ 0,6` (une vraie colonne n'a de sens que dans une bande verticale).
2. **Hauteur de rangée minimale imprimée : 30 mm** (en dessous, une photo est un timbre-poste ; pénalité ci-dessus, ou rejet dur).
3. **Secours = grille équilibrée, jamais la colonne** : remplacer `part = [1] * n` (624) par des rangées de `ceil(sqrt(n))` (4 → [2,2], 5 → [3,2]) — ou réutiliser `_grid_layout` (652-698) qui fait ce travail correctement.
4. **Plancher de couverture** : si le meilleur score d'aire reste < 0,55 (cas structurel « 4 portraits en A4 paysage » : 0,41 même bien calepiné), c'est la densité qui est fausse, pas la partition → reporter 1 photo sur la page suivante (le chunking par jour de `_program_chunks` le permet) ou promouvoir la meilleure en pleine page. C'est ce qu'un maquettiste ferait.

---

## 6. PRIORISATION — par impact sur l'objet imprimé

### Quick wins (heures → 1 jour chacun)

| # | Action | Où | Impact |
|---|---|---|---|
| 1 | **Chronologie** : migration `sort_mode`, suppression de la bascule 4384, tri inconditionnel | app.py:3348, 4384 + 1 UPDATE | Majeur — l'ordre du récit est le squelette du livre ; corrige aussi les faux bandeaux de jour |
| 2 | **Calepinage** : score en aire, filtre post-échelle, secours en grille, plancher 30 mm | pdf_book.py:576-650 | Majeur — supprime la colonne de portraits et toutes ses variantes |
| 3 | **Légendes étanches** : re-dessin inline des captions non placées (1210-1211), bandeau caption sur spread (1029-1054), vidéos à 2 lignes (537), ellipse sur troncature (362) | pdf_book.py | Fort — plus aucun texte d'auteur perdu |
| 4 | **Tuiles CARTO voyager @2x** dans `_fetch_osm_tile` + clé de cache + attribution CARTO | app.py:2018-2045, pdf_book.py:1020 | Fort — la carte du livre ressemble enfin à celle de l'app, et net à 300 dpi |
| 5 | **Couverture** : dates en français, composition resserrée (photo 55 %→ titre accolé), garde-fou « carte indisponible » | pdf_book.py:949-951, 905-965, 1000 | Fort — c'est la première ET la dernière impression |
| 6 | Bleed permanent quel que soit le contenu | pdf_book.py:268-269 | Moyen — fiabilité imprimeur |

### Chantiers (2 – 5 jours chacun)

| # | Chantier | Contenu | Impact |
|---|---|---|---|
| A | **Carte de livre** (§4) : `_build_book_map` Pillow — pins vignettes photo, pastilles pin_kind + emoji, étiquettes de villes avec halo et anti-collision, tracé jour par jour, badges de jour | app.py (nouveau) + pdf_book overview_map | Le plus visible : la page que le couple ouvre en premier |
| B | **Colonne timeline** du planning à gauche/droite (réglage `pdf_map_timeline_side`), double page carte | app.py schéma + carnet_pdf_settings + pdf_book | Complète le chantier A |
| C | **Épingles `photo_notes` imprimées** : jointure `_carnet_pages`, appels de note ①② sur photo + texte attribué en marge (convention §2.1) | app.py:3315 + pdf_book:_draw_image_box / margin_entries | La demande verbatim n° 1 du client |
| D | **Architecture du livre** : page de garde + page de titre, ouverture de chapitre par jour (pleine page), page de statistiques, 4e de couverture, pagination belle-page | pdf_book programme 829-887 | Transforme un « export PDF » en livre |
| E | Divers commentaire : caption+texte cumulés en marge (1177), vidéos de marge visibles (1232-1246), avertissements de troncature dans l'aperçu | pdf_book + apercu | Finition |

**Règle de conduite** : les quick wins 1-3 sont des correctifs de confiance — tant qu'un livre peut perdre des textes ou inverser des jours, aucune beauté ne rattrape l'objet. Les chantiers A/B/C sont la réponse directe aux verbatims. Le chantier D est ce qui fera dire « on dirait un vrai livre ».
