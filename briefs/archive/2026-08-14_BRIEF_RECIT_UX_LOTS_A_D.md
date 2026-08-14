# BRIEF « Récit » — refonte UX album & fil — EXÉCUTÉ le 2026-08-14 (nuit autonome)

> Brief fourni par Arthur dans la conversation Cowork du 14/08 au soir (« App Récit :
> album de voyage chronologique, conversation & prévision », v1.0), avec mandat
> d'exécution autonome sur 8 h : développement, vérification, correction, suite.
> Ce fichier archive le brief EN L'ÉTAT où il a été exécuté, annoté de ce qui a été
> livré, décidé et laissé pour la suite. 4 commits : v5.7 → v5.10.

## Ce que le brief demandait vs ce qui existait déjà

Le brief a été écrit sans lecture du dépôt (« stack supposée — corriger si différent »).
Constat d'entrée : les miniatures 400 px, le clustering jour/lieu (cas A/B/C), le
géocodage Nominatim caché, l'EXIF client+serveur, les notes de marge ancrées, l'import
Hinge + chat live, les rêveries→voyage et les trajets OSRM existaient déjà. Le manque
réel : la présentation (grille, héro, repli, densité), la chronologie des photos sans
EXIF, les épingles sur photo, le fil unifié avec charnière, et le lieu éditable.

## Livré

### v5.7 — Lot A + B : « les photos encombrent » + chronologie fiable (commit 551f531)
- Grille **justified** à rangées régulières (flex, `--ar` par tuile, hauteur `--row-h`),
  gouttières 3 px desktop / 2 px mobile, fin du polaroid (rotations, ombres, chevauchements).
- **Photo héro** par scène (≥3 photos) servie en **1024 px** (`mid_path`, généré à
  l'upload pour le neuf, paresseusement pour l'existant via `_ensure_mid`).
- **Repli** des scènes ≥10 pages au-delà de 7 (« + N photos » / « Replier »).
- **Densité** Récit / Confort / Dense, mémorisée par appareil (localStorage).
- En-têtes de jour **collants** translucides (blur), typo UI bold, fin des italiques.
- Légendes déplacées dans le panneau outils (popover) ; pastille discrète si légende.
- **Date devinée du nom de fichier** quand l'EXIF est strippé (WhatsApp/Pixel/Screenshot…)
  — testée sur 15 motifs, refus des dates futures ; **badge ≈** à l'écran (repli bruyant, R4).
- **Boîte « À dater »** : bandeau d'entrée + atelier de datation **par lot**
  (sélection multiple, 1 min d'écart pour garder l'ordre, source tracée `manuel`,
  recompute immédiat). La datation unitaire recompute aussi désormais.
- `taken_at_source` + `orig_filename` en base (migrations idempotentes).
- **Icône de l'app** (cœur terracotta sur crème) : favicon SVG/PNG, apple-touch-icon,
  manifest PWA 192/512 + maskable (les anciens chemins pointaient vers des fichiers absents).

### v5.8 — Lot C : épingles sur photo (commit 5baad57)
- Table `photo_notes(photo_id, x, y, texte, auteur_id)` — coordonnées **normalisées 0-1**,
  validées serveur.
- Lightbox : bouton « Épingler une note », couche d'épingles calée sur la zone visible
  de l'image (`object-fit: contain` compensé), popover lecture (auteur + date),
  **couleur par membre**, retrait par l'**auteur seul** (403 sinon — testé).
- Compteur discret sur la vignette, mis à jour sans rechargement.

### v5.9 — Lot D : le fil unifié + date charnière (commit e99dcab)
- La page Histoire devient **la timeline du récit** : messages (inchangés, chapitres
  compris) + **journées de photos** en mosaïque (tous carnets, 12 max + « +N », lien
  album) + **départs de voyage** + **bloc charnière monumental** (grande date en toutes
  lettres formatée côté serveur, titre court, « À la suite, en photos »).
- Charnière posable/corrigeable/retirable depuis la page (`couples.date_charniere`).
- En mode recherche `?q=`, le fil redevient messages seuls.

### v5.10 — le lieu, complément du Lot B (commit af0d1fe)
- « 📍 Définir / Corriger le lieu » dans le panneau outils d'une photo : recherche
  via `/geo/search` (Nominatim existant, caché), label choisi **prioritaire** sur le
  reverse-geocoding, `lieu_source` tracée, sections recalculées dans **tous** les
  carnets où la photo vit.
- **HEIC iPhone** : `pillow-heif` en requirements, enregistrement optionnel et
  **bruyant** dans les logs s'il manque.

## Vérifications (batteries passées avant chaque commit)
- Syntaxe `ast.parse` (app.py + pdf_book.py) ; boot ×2 (migrations idempotentes
  rejouées sur une copie de la base réelle) ; 0 doublon de fonction/route.
- Smoke final : **22 GET, 0×500** ; `/carnet/16/pdf` → PDF 85 Ko OK (second ordre
  aperçu/livre vérifié). Routes : 86 → **91** (+5 : dater_lot, notes ×2, lieu, charnière).
- Tests négatifs par lot : CSRF 403, date pourrie 400, coords hors bornes 400,
  photo/carnet d'un **autre espace** 404 (cloisonnement), suppression d'épingle par
  un non-auteur 403 (la note survit, D4), page intruse dans un lot de datation ignorée.
- Vérification **visuelle réelle** (Chromium) : desktop + mobile 390 px, pose et
  lecture d'épingle, dépliage de scène, en-têtes collants, fil + charnière. 0 erreur JS.
- Reverse-geocoding **moqué** dans les tests (pas d'appel réseau, piège documenté).

## Décisions prises en route (une ligne chacune)
- Direction « Apple sobre » appliquée à l'ALBUM d'abord (validé par Arthur au cadrage) ;
  le reste de l'app garde la charte crème — cohabitation propre, bascule complète plus tard.
- Le fil unifié vit dans `/histoire` (pas de nouvelle URL — les routes sont des contrats).
- Date sans heure devinée d'un nom de fichier → 12:00:00 (neutre dans la journée).
- Héro seulement si ≥3 photos ; repli seulement si ≥10 pages (pas de « +1 photo » ridicule).
- Épingle supprimable par son auteur uniquement (couple : pas de hiérarchie owner ici).
- Pas de Lot E (budget/Tricount) cette nuit : c'est de l'ARGENT → arbitrages produit
  à faire avec Arthur (statuts, devises, répartition) plutôt qu'en autonomie.

## Limites dites honnêtement
- Les photos d'AVANT v5.7 n'ont pas de `orig_filename` (nom perdu au renommage token) :
  le fallback nom de fichier ne vaut que pour les uploads futurs ; l'atelier « À dater »
  couvre l'existant.
- `_ensure_mid` retente à chaque affichage si l'original manque (log warning à chaque
  rendu — visible, pas bloquant).
- Densité mémorisée par APPAREIL (localStorage), pas par compte.
- Drag & drop inter-listes (Sortable `group`) testé en code, pas au doigt sur téléphone réel.
- Pas testé en prod : `pillow-heif` s'installera au build Railway (wheel dispo linux/x86).
- Le badge ≈/✎ n'apparaît que sur les nouvelles sources ; l'historique daté reste sans badge
  (impossible de distinguer EXIF/manuel a posteriori — on ne ment pas).

## Reste à faire (proposé, dans l'ordre)
1. Lot E : budget 3 états (estimé/réservé/payé) + répartition entre membres — à cadrer.
2. Étendre la direction sobre aux autres écrans (accueil, rêveries, fil) si validée à l'usage.
3. Doublons par hash perceptuel (imagehash) — proposition de fusion, jamais auto.
4. Purge optionnelle des originaux (garder 1024) + quota par récit (stockage ×1,15).
5. `_to_delete/` à la racine : déchets git du pont Cowork (locks/objets temporaires) — à
   supprimer à la main de temps en temps ; ignoré par git.
