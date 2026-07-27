# BRIEF — Une épingle doit pouvoir appartenir à plusieurs jours (multi-trajets)

> ✅ **Exécuté le 2026-07-27** (v5.1). Tous les points livrés.
> Notes d'exécution :
> - §4.4 : l'album lit `trajet_steps` et chaque occurrence porte SON jour
>   (`planned_day` par occurrence) — le JS `album.html` fonctionne tel quel, non touché.
>   `etapes_all` (compteur + ancres de notes) reste dédoublonné : une note ancrée
>   sur une étape partagée pointe sur son PREMIER jour.
> - §4.5 : déduplication faite côté serveur ET côté client (ceinture + bretelles).
> - Mode COPIE de la transformation : l'`INSERT..SELECT` en bloc est devenu une
>   boucle, seul moyen de récupérer l'id de chaque copie pour recâbler ses jours.
> - **Hors brief, trouvé en route** : `carnet_transformer` ne vérifiait pas que les
>   `item_ids` reçus appartenaient au carnet source — un POST forgé copiait (et en
>   mode déplacement VOLAIT) un item d'un autre espace. Fermé ici (D1/R1), test de
>   non-fuite ajouté.
> - Non fait : §5.6 (backfill vérifié sur une copie de la DB PROD). La seule sauvegarde
>   prod locale date du 2026-05-16, antérieure à `planned_day` — le backfill est donc
>   testé sur données synthétiques, pas sur le vrai volume. À revérifier après déploiement.

Priorité : haute · Périmètre : rêveries (carte + planning-perles) et retombées voyage/album
Rédigé par Fable (session v5.0, 2026-07-26) pour exécution par Claude Opus.
Lire d'abord `CLAUDE.md` (workflow briefs, vérifs bash, confirmation avant commit/push).

## 1. Le bug (reproduction)

Dans une rêverie avec jalon (ex. 07/08 → 12/08) :
1. Tracer le parcours A → B, l'attribuer au **jour 1**.
2. Tracer le parcours B → C, l'attribuer au **jour 2**.
3. 💥 **B disparaît du jour 1.** Attendu : B termine le jour 1 ET ouvre le jour 2
   (cas type : B = la nuit d'étape — on y arrive le soir, on en repart le matin).

## 2. Cause racine (analyse faite, vérifiée)

C'est un plafond du **modèle de données**, pas un bug d'UI :

- `carnet_items.planned_day` est un **INTEGER unique** par item
  (migration v4.5 dans `app.py`, liste `migrations`). Une épingle ne peut
  mathématiquement appartenir qu'à un seul jour.
- Côté client, `assignRouteToDay(k)` dans `templates/carnet_souhait.html`
  retire volontairement les épingles du tracé de tous les autres jours
  (`planDays = planDays.map(day => day.filter(id => ids.indexOf(id) < 0))`)
  avant de poser `planDays[k] = ids` — décision « un trajet par jour » v5.0
  qui découlait du modèle mono-jour.
- `carnet_planning_save` (`app.py`, route `/carnet/<id>/planning`) écrit ce
  modèle : `UPDATE carnet_items SET planned_day=?, position=?` par item.

## 3. Modèle cible

Introduire une vraie table de pas de trajet (un item peut apparaître dans
plusieurs jours, et une seule fois par jour) :

```sql
CREATE TABLE IF NOT EXISTS trajet_steps (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    carnet_id  INTEGER NOT NULL REFERENCES carnets(id) ON DELETE CASCADE,
    day        INTEGER NOT NULL,          -- index 0-based dans le jalon
    position   INTEGER NOT NULL DEFAULT 0,
    item_id    INTEGER NOT NULL REFERENCES carnet_items(id) ON DELETE CASCADE,
    UNIQUE(carnet_id, day, item_id)
);
CREATE INDEX IF NOT EXISTS idx_trajet_steps ON trajet_steps(carnet_id, day, position);
```

- Migration idempotente dans la liste `migrations` de `init_db()` (pattern
  maison : `CREATE TABLE IF NOT EXISTS`). **Backfill** : pour chaque item avec
  `planned_day IS NOT NULL`, insérer une ligne `trajet_steps` (day=planned_day,
  position=position). Ne PAS supprimer la colonne `planned_day` (rétro-compat,
  et SQLite n'aime pas les DROP COLUMN) — elle devient dérivée : « premier jour
  où l'item apparaît », maintenue à l'écriture pour l'affichage des épingles.

## 4. Travaux

### Backend (`app.py`)

1. Migration + backfill ci-dessus.
2. `carnet_planning_save` : le JSON d'entrée ne change pas
   (`{date_start, date_end, days: [[item_id,...], ...]}` — un item peut
   maintenant apparaître dans plusieurs sous-tableaux). Écriture :
   `DELETE FROM trajet_steps WHERE carnet_id=?` puis INSERT par (day, pos).
   Mettre à jour `planned_day` dérivé = premier jour d'apparition (et
   `position` = position dans ce premier jour), NULL si absent partout.
3. `carnet_souhait_view` : construire `days_steps` = liste de listes d'item_ids
   depuis `trajet_steps` (fallback : dériver de `planned_day` si table vide)
   et le passer au template. `geo_items` : ajouter `planned_days` = liste des
   jours de l'épingle (pour la couleur et le popup).
4. `carnet_album` (voyage) : `etapes_by_day` lit `trajet_steps` en priorité
   (une étape peut figurer dans deux jours du squelette de l'album — c'est
   voulu : « nuit au gîte » apparaît le soir du J1 et le matin du J2).
   Après `carnet_transformer`, les `trajet_steps` doivent suivre : réécrire
   `carnet_id` des steps vers le nouveau carnet pour les items déplacés
   (mode duplicate : recréer les steps en pointant les copies).
5. Transformation (`carnet_transformer`, appel `goVoyage`) : l'ordre envoyé =
   concat des jours ; **dédupliquer en gardant la première occurrence** avant
   le déplacement des items (un item ne se déplace qu'une fois).

### Front (`templates/carnet_souhait.html`)

6. `planDays` s'initialise depuis `days_steps` (plus depuis
   `GEO_ITEMS.planned_day`).
7. `assignRouteToDay(k)` : **supprimer le retrait des épingles des autres
   jours**. Le tracé remplace uniquement le jour k. Garder l'unicité DANS un
   jour (un id une seule fois par jour).
8. Pool « à caser » = items présents dans **aucun** jour.
9. `stepToPool(id)` ne retire que du jour ouvert (`selectedDay`), pas partout.
10. Épingle multi-jours : couleur = premier jour ; si l'épingle appartient à
    plusieurs jours, ajouter un petit liseré blanc pointillé (classe
    `pin-multi`) et lister les jours dans le popup (« Jour 1 · Jour 2 »).
11. `planRefreshPinDays` : dériver depuis `planDays` (premier jour trouvé).

### Non-régression à préserver

- Tracés permanents par jour (`updateDayRoutes`) fonctionnent tels quels
  (ils lisent `planDays`) — vérifier que B partagé dessine bien les DEUX
  polylines qui se touchent en B.
- Sauvegarde auto (debounce 700 ms), perles, jour déplié, évidence croisée.
- Aucune modification de `schema.sql` (la vérité est `init_db()`).

## 5. Tests à jouer (local, DB copiée hors du mount — voir CLAUDE.md)

1. A→B jour 1, B→C jour 2 : **B présent dans les deux jours**, deux tracés
   dessinés, temps par jour corrects, perles avec bons compteurs.
2. Relecture serveur (reload) : planning intact, B toujours dans les 2 jours.
3. `stepToPool(B)` depuis le jour 2 : B reste au jour 1.
4. « Passer en voyage » : items dédupliqués (B déplacé une fois), ordre =
   J1 puis J2 sans doublon, `trajet_steps` recâblés sur le voyage,
   `etapes_by_day` de l'album montre B au J1 ET au J2.
5. Batterie maison : `ast.parse`, `from app import app`, pas de fonctions
   dupliquées, rendu 200 sur `/carnet/5/souhait`, `/carnet/6/album`,
   `/carnet/6/apercu`, `/carnet/6/pdf?...`.
6. Vérifier le backfill sur une copie de la DB prod locale (232 pages,
   items avec planned_day existants).

## 6. Definition of done

- Le scénario §1 passe (B dans les deux jours, visible carte + perles).
- Migration idempotente jouée deux fois sans erreur.
- Commit unique `feat(v5.1): epingle multi-jours - table trajet_steps, ...`
  proposé à Arthur AVANT commit/push (règle CLAUDE.md).
- Ce brief déplacé vers `briefs/archive/2026-XX-XX_BRIEF_EPINGLE_MULTI_JOURS.md`.

## 7. Pièges connus (appris dans les sessions précédentes)

- Le mount FUSE casse les locks git : si commit échoue, `mv` les
  `.git/*.lock` vers `.git/_stale_locks/` puis réessayer (jamais `rm`).
- `csrf_check()` accepte `X-CSRF-Token` en header (les POST JSON du planning
  l'utilisent).
- Les templates rendent `GEO_ITEMS` en littéral JS manuel : toute nouvelle
  donnée par épingle doit être ajoutée À LA FOIS dans la vue python et dans
  ce littéral (bug déjà rencontré en v4.5 avec planned_day).
- Tester avec Playwright en interceptant `/geo/route` (OSRM inaccessible
  depuis certains environnements) — pattern des sessions v4.x.
