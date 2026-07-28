# Design — La journée en blocs (v5.2)

> Brainstormé avec Arthur le 2026-07-28. Décisions validées en séance, notées ici pour
> qu'on n'ait pas à les redécouvrir. Suite directe de la v5.1 (épingle multi-jours).

## Le besoin

1. Voir le **temps total de trajet par jour** (aujourd'hui : seulement le trajet unique du jour, et toujours calculé en voiture même si on a tracé à pied).
2. Pouvoir **épingler deux fois le même lieu dans un trajet** (le retour : A→B→A).
3. Pouvoir mettre **plusieurs trajets par jour, chacun son mode** : aller en voiture, boucle à pied, retour en voiture.
4. Une **vue journée** où poser un restaurant à midi.

Ces quatre demandes sont une seule question : qu'est-ce qu'une journée.

## La réponse : la journée est une suite de BLOCS

Un bloc = un mode + une heure optionnelle + des étapes ordonnées.

- Bloc à **≥ 2 étapes** = un **trajet** : tracé sur la carte, temps calculé, couleur du jour.
- Bloc à **1 étape** = une **halte** : le restaurant de midi. Ni tracé ni temps, juste un lieu et son heure.

Temps total du jour = somme des durées des blocs à ≥ 2 étapes.

Décidé en séance : la journée reste une **suite ordonnée** (pas un agenda horaire) ; l'heure
est **optionnelle et décorative** — elle ne réordonne jamais la journée, l'ordre reste manuel.
Tout ce qui entre dans une journée est une **épingle** (pas de nouveau type d'élément).

## Modèle de données

```sql
CREATE TABLE trajets (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    carnet_id  INTEGER NOT NULL REFERENCES carnets(id) ON DELETE CASCADE,
    day        INTEGER NOT NULL,             -- index 0-based dans le jalon
    ordre      INTEGER NOT NULL DEFAULT 0,   -- position du bloc DANS la journée
    mode       TEXT NOT NULL DEFAULT 'car',  -- 'car' | 'foot' | 'bike'
    heure      TEXT NOT NULL DEFAULT ''      -- 'HH:MM' ou ''
);
CREATE TABLE trajet_steps (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    trajet_id  INTEGER NOT NULL REFERENCES trajets(id) ON DELETE CASCADE,
    position   INTEGER NOT NULL DEFAULT 0,
    item_id    INTEGER NOT NULL REFERENCES carnet_items(id) ON DELETE CASCADE
);
```

**Aucune contrainte d'unicité sur `item_id`.** L'identité d'une étape est sa *position*,
pas son item. C'est ce qui rend A→B→A possible.

`carnet_items.planned_day` reste une valeur **dérivée** (premier jour d'apparition) :
couleur des épingles, album, rétro-compat.

### Migration (la seule opération irréversible du lot)

`trajet_steps` v5.1 porte `UNIQUE(carnet_id, day, item_id)` ; SQLite ne sait pas retirer
une contrainte → reconstruction, dans UNE transaction :

1. `CREATE TABLE IF NOT EXISTS trajets` + `trajet_steps_v2` ;
2. si l'ancienne `trajet_steps` a la colonne `carnet_id` (donc = v5.1) : pour chaque
   `(carnet_id, day)` distinct → **un bloc** `mode='car'`, étapes recopiées dans l'ordre ;
3. compter avant / après — **si les comptes divergent, ROLLBACK et log d'erreur** ;
4. `DROP` l'ancienne, `ALTER TABLE ... RENAME` la neuve.

Idempotente : détection par `PRAGMA table_info`, rejouable sans effet.

## Gestes (mobile d'abord)

- **Carte → journée** : tracer un parcours, « Attribuer ce trajet », choisir le jour →
  **ajoute un bloc** en fin de journée, avec le mode choisi dans la barre de tracé.
  (Avant : le trajet REMPLAÇAIT la journée.)
- **Tracer un aller-retour** : en mode tracé, toucher une épingle déjà dans le parcours
  l'**ajoute à nouveau** (avant : ça la retirait). Réparation d'un faux geste : bouton
  **« ↩ Dernière étape »** dans la barre de tracé, plus « Annuler » qui vide tout.
- **Pool → journée** : glisser une épingle dans un bloc l'y ajoute ; la déposer dans la
  zone « halte » de la journée crée un bloc à 1 étape (le restaurant). **Ajouté en cours
  de route** : chaque idée du pool porte aussi un **＋** quand un jour est ouvert — glisser
  dans une page qui défile est pénible au doigt (D3), et le glisser reste possible.
- **Dans un bloc** : réordonner par glisser, ✕ retire **cette occurrence seulement**,
  sélecteur de mode 🚗🚶🚲, champ heure optionnel, ✕ du bloc supprime le bloc entier.
- Un bloc vidé de ses étapes disparaît ; une journée sans bloc revient à son état vide.

## Affichage

- **Perle du jour** (fermée) : le compteur d'étapes + le **temps total du jour**.
- **Journée dépliée** : date, temps total, puis les blocs dans l'ordre — chacun avec son
  mode, son heure si posée, sa durée, ses étapes.
- **Carte** : un tracé par bloc à ≥2 étapes, couleur du jour ; les blocs d'un même jour
  partagent la couleur et se distinguent par leur mode (trait plein voiture, pointillé
  à pied, tirets vélo).

## Cache des itinéraires

`/geo/route` n'a aucun cache : chaque affichage rejoue un appel OSRM par bloc, sur un
service public en fair use. On ajoute :

```sql
CREATE TABLE route_cache (
    key        TEXT PRIMARY KEY,   -- sha1(profile + coords arrondies 5 déc.)
    profile    TEXT NOT NULL,
    duration_s INTEGER, distance_m INTEGER,
    geometry   TEXT,               -- JSON
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

Lecture avant appel, écriture après succès. Un échec OSRM n'écrit rien et reste **bruyant**
(la journée affiche « temps indisponible », pas un zéro silencieux).

## Ce que ça garde ouvert : les photos (prochain lot)

Décidé : **pas dans ce lot**, mais le modèle ne doit rien bloquer. Les blocs sont
rattachés au carnet, suivent le passage en voyage, portent `day` + `heure`, et leurs
étapes sont géolocalisées. Les photos portent déjà `taken_at`, `gps_lat/lng`, ville.
Le rattachement photo ↔ bloc (heure la plus proche + épingle la plus proche) pourra
s'ajouter par une simple table de liens, sans toucher à ce schéma.

## Erreurs et états vides

- Aucun jalon posé → la journée n'existe pas : message, pas de bloc fantôme.
- OSRM indisponible → durée absente et DITE, jamais 0 ni un temps inventé.
- Épingle supprimée → ses étapes tombent (CASCADE), les blocs vides disparaissent.
- POST d'un client resté ouvert sur l'ancien format (`days: [[item_id, ...]]`) → accepté
  et interprété comme un bloc unique par jour, pas un 500.

## Tests

Batterie `test_journee_blocs.py`, sur copie de la DB, avec au moins :

1. A→B→A dans un bloc : l'item revient deux fois, aux positions 0 et 2.
2. Trois blocs dans un jour (car / foot / car) : ordre et modes relus intacts.
3. Temps total du jour = somme des blocs à ≥2 étapes (halte ignorée).
4. Halte avec heure : 1 étape, pas de trajet, heure relue.
5. Migration v5.1 → v5.2 : un bloc par (jour) existant, comptes conservés, rejouée 3× sans dérive.
6. Négatif : item d'un autre espace refusé ; bloc d'un autre carnet inaccessible.
7. Passage en voyage : blocs et modes suivent, déduplication des items conservée.
8. Cache : deux appels identiques → un seul appel OSRM.
9. Ancien format de POST toujours accepté.
10. Smoke : rêverie, album, aperçu, PDF.
11. **Invariant v5.1 conservé** : la même épingle dans deux jours (la nuit d'étape).
    La batterie v5.1 interrogeait le schéma plat et devient caduque ; son invariant
    métier est **porté** ici, pas abandonné.

## Journal d'exécution (2026-07-28)

Livré. 45 assertions vertes. Ce que la vérification au navigateur a corrigé, et qui
ne se serait pas vu autrement :

- `selectDay()` ne redessinait pas le pool → le **＋** n'apparaissait jamais.
- La durée du bloc se cassait en trois lignes, puis se faisait tronquer en « ≈ 3… »,
  puis l'heure « 09:30 » s'affichait « 09: ». Trois passes de largeurs sur l'en-tête.
- Le texte de la modale d'attribution mentait encore (« devient celui du jour choisi »).
- La tolérance à l'ancien format était au mauvais niveau : l'ancien format envoie une
  journée **plate**, pas une liste de blocs (attrapé par le test 9).

**Limite dite** : le glisser-déposer vers la zone « halte » n'a pas pu être éprouvé en
automation (SortableJS ignore un drag synthétique). Le chemin par **＋** l'est, et la
persistance des haltes aussi. À confirmer au doigt sur un vrai téléphone.
