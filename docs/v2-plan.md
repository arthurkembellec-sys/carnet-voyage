# V2 — Plan d'exécution « Notre Histoire »

> Plan demandé en §24 de l'addendum V2 (`brief_app_couple_v2.md`).
> Date : 2026-05-02 — auteur : Claude Code.
> **Statut** : à valider par Arthur AVANT tout code V2.

---

## 0 · Pré-requis

Le brief V2 §0 (préambule) est explicite :
> « Position dans la roadmap : ces fonctionnalités constituent la V2. Ne pas les implémenter avant que la V1 (création de carnet de voyage → album → impression) soit pleinement fonctionnelle et validée par les utilisateurs. »

**État V1 actuel** (au 2026-05-02) :

| Patch | Statut | Notes |
|---|---|---|
| v1.0 couple | ✅ live | déployé, testé |
| v1.1 carnets | ✅ live | déployé, testé |
| v1.2 album | 🟡 en cours | déployé, fix multi-photos en cours de validation (commit `03d2e1e`) |
| **v1.3 PDF** | ❌ pas livré | **bloquant V2** |
| **v1.4 profil** | ❌ pas livré | bloquant V2 (allégeable, voir §6) |

→ Reste à coder pour clore V1 : **patches v1.3 et v1.4**, ~1.5 jour estimé.

---

## 1 · Synthèse du périmètre V2

Trois domaines fonctionnels distincts, mais cousus dans le même tissu visuel :

### A. Histoire & Conversations *(brief §14)*
Fil chronologique unifié = archive Hinge importée (immuable) + messages live échangés dans l'app. Onglet dédié, composer en bas, support photo/lien.

### B. Rêveries *(brief §15)*
Carnet incubateur : pas d'album imprimable, juste une liste d'items hétérogènes (lien, photo, note, lieu, budget). Brouillon assumé visuellement.

### C. Transformation rêverie → voyage *(brief §16)*
Atomique : sélection d'items → fiche carnet → migration des items (déplacés, pas copiés sauf option). Rêverie passe en `transformed` puis `completed` quand le voyage est verrouillé.

---

## 2 · Modèle de données — récap des nouvelles tables

Conformément au brief §17, **2 tables séparées** pour `Carnet` vs `Reverie` (recommandation par défaut du brief, validée par la nature très différente de leurs items).

### Nouvelles tables

```sql
-- Histoire / Conversations (V2.0)
CREATE TABLE conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    couple_id INTEGER NOT NULL UNIQUE REFERENCES couples(id) ON DELETE CASCADE,
    archive_imported_at TIMESTAMP,
    archive_source TEXT DEFAULT '',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE chapters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    position INTEGER NOT NULL DEFAULT 0,
    title TEXT NOT NULL,
    headline TEXT DEFAULT '',
    date_label TEXT DEFAULT '',
    weekday_label TEXT DEFAULT '',
    featured_image_url TEXT DEFAULT '',
    image_caption TEXT DEFAULT ''
);

CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK(kind IN ('archived','live')),
    chapter_id INTEGER REFERENCES chapters(id) ON DELETE SET NULL,
    sender_type TEXT CHECK(sender_type IN ('userA','userB','system')),
    sender_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    sender_label TEXT DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    attachment_type TEXT CHECK(attachment_type IN ('photo','link','voice','carnet_ref') OR attachment_type IS NULL),
    attachment_ref TEXT,
    sent_at TIMESTAMP NOT NULL,
    edited_at TIMESTAMP,
    deleted_at TIMESTAMP
);

-- Rêveries (V2.1)
CREATE TABLE reveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    couple_id INTEGER NOT NULL REFERENCES couples(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    description TEXT DEFAULT '',
    cover_photo_id INTEGER REFERENCES photos(id) ON DELETE SET NULL,
    status TEXT NOT NULL DEFAULT 'dreaming'
        CHECK(status IN ('dreaming','transformed','completed','archived')),
    created_by INTEGER NOT NULL REFERENCES users(id),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE reverie_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reverie_id INTEGER REFERENCES reveries(id) ON DELETE CASCADE,
    target_carnet_id INTEGER REFERENCES carnets(id) ON DELETE SET NULL,
    position INTEGER NOT NULL DEFAULT 0,
    kind TEXT NOT NULL CHECK(kind IN ('link','photo','note','location','budget')),
    title TEXT DEFAULT '',
    body TEXT DEFAULT '',
    url TEXT DEFAULT '',
    photo_id INTEGER REFERENCES photos(id) ON DELETE SET NULL,
    address TEXT DEFAULT '',
    geo_lat REAL,
    geo_lng REAL,
    amount REAL,
    currency TEXT DEFAULT '',
    added_by INTEGER NOT NULL REFERENCES users(id),
    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    -- Invariant : reverie_id XOR target_carnet_id (exactement un des deux)
    CHECK ((reverie_id IS NULL) <> (target_carnet_id IS NULL))
);
```

### Modification de table existante

```sql
ALTER TABLE carnets ADD COLUMN parent_reverie_id INTEGER
    REFERENCES reveries(id) ON DELETE SET NULL;
```

→ Pas de modification de l'enum `type` côté carnets : on **garde** voyage/restaurant/sortie/autre. Les rêveries vivent dans leur table dédiée.

### Migration au boot V2

- Pour chaque couple existant → créer une `conversation` vide (sans archive)
- Aucune donnée existante n'est cassée — V2 purement additive

---

## 3 · Découpage en patches

Suit l'ordre §19 du brief. Chaque patch = autonome, mergeable, déployable.

### V2.0 — Conversations *(patch `v2_0_conversations.py`)*

1. Migrations : `conversations`, `chapters`, `messages`
2. Backfill : 1 conversation par couple existant
3. Routes :
   - `GET /histoire` — fil unifié (archive + live)
   - `POST /histoire/message` — ajout message live (texte, lien, photo)
   - `POST /message/<id>/modifier` — édition (auteur uniquement)
   - `POST /message/<id>/supprimer` — soft delete
   - `GET /histoire/import` — page admin import archive (JSON)
   - `POST /histoire/import` — traitement upload JSON
4. Templates : `histoire.html`, `histoire_import.html`, partial `_message.html`
5. Topbar : nouvel onglet « Histoire » à côté de « Carnets »

**DoD V2.0** : 2 partenaires écrivent dans le fil, photos jointes affichées, archive importée affichée en chapitres immuables.

### V2.1 — Rêveries *(patch `v2_1_reveries.py`)*

1. Migrations : `reveries`, `reverie_items`
2. Routes :
   - `GET /reverie/nouvelle` + POST
   - `GET /reverie/<id>` — détail avec grille items
   - `POST /reverie/<id>/item` — ajout item (multi-type)
   - `POST /reverie_item/<id>/modifier` + suppression
   - `POST /reverie/<id>/items/reorder` — drag & drop (V2.1.1, peut différer)
3. Filtres accueil : ajouter chip « Rêveries » à côté des types existants
4. Templates : `reverie_form.html`, `reverie_view.html`
5. Composant card « brouillon » (bordure pointillée, fond texturé)

**DoD V2.1** : créer une rêverie, ajouter 5 items de types différents, voir dans la liste, filtrer.

### V2.2 — Transformation *(patch `v2_2_transformation.py`)*

1. Migration : `ALTER TABLE carnets ADD COLUMN parent_reverie_id`
2. Routes :
   - `GET /reverie/<id>/transformer` — écran sélection items
   - `POST /reverie/<id>/transformer` — atomique : crée carnet + déplace items
3. Affichage badge « ✨ née d'une rêverie » sur la fiche carnet
4. Section « Voyages issus » sur la fiche rêverie transformée
5. Hook : quand carnet passe `locked` ET rêverie n'a plus d'items → status `completed`

**DoD V2.2** : transformer une rêverie en voyage, voir le lien parent-enfant, vérifier que les items déplacés ne sont plus dans la rêverie.

### V2.3 — Polish *(patch `v2_3_polish.py`, optionnel)*

- Mention `@carnet` dans les messages
- Recherche textuelle simple dans la conversation (LIKE SQL)
- Stats sur le profil (nb rêveries, voyages, messages)
- Pas de notes vocales V2 (reporter V3)

---

## 4 · Estimation

| Étape | Estimation |
|---|---|
| Finir V1 (patches 1.3 PDF + 1.4 profil) | ~1.5 jour |
| **V2.0 Conversations** | ~2 jours |
| **V2.1 Rêveries** | ~1.5 jour |
| **V2.2 Transformation** | ~1 jour |
| V2.3 Polish | ~0.5 jour |
| **Total V2** | **~5 jours** |

---

## 5 · Points d'attention spécifiques V2

### A. Immuabilité de l'archive
Le brief §21.1 est strict :
> « L'archive Hinge importée doit être traitée comme la donnée la plus précieuse de l'app. Aucune feature ne doit pouvoir l'altérer. »

→ Toutes les routes d'écriture vérifient `kind != 'archived'`. Tests dédiés : tenter de PATCH/DELETE un message archived doit retourner 403.

### B. Transformation atomique
Brief §21.2 :
> « La transformation rêverie → voyage est une opération atomique : soit tout réussit, soit rien ne change. »

→ Wrapper l'opération dans une transaction SQLite : `with conn: conn.execute("BEGIN")` … `conn.execute("COMMIT")`. Rollback si la moindre erreur.

### C. Cohérence visuelle V1 ↔ V2
Brief §21.4 :
> « Les conversations, rêveries et voyages doivent se sentir cousus dans le même tissu — même typo, mêmes couleurs, mêmes espacements. »

→ Réutiliser massivement les composants CSS V1 (Card, Button, Chip, etc.). Pas de nouvelle palette. Les rêveries ont un traitement « brouillon » mais avec les **mêmes variables CSS**.

### D. Format JSON archive
Le format est défini §22 du brief. Retenu tel quel pour V2.0. Bouton « Importer une archive » dans le profil + page admin dédiée.

---

## 6 · Différé / hors-scope V2

Conforme brief §20, **et en plus** :
- ❌ Notes vocales (V3, brief §19.3 V2.3 mais on les sort de V2)
- ❌ Drag & drop items rêverie : optionnel V2.1, peut attendre V2.1.1
- ❌ Preview automatique des liens (OG image/title) : V2.0 minimal = juste afficher l'URL, V2.0.1 ajoutera la preview
- ❌ Notifications partenaire (V3)

---

## 7 · Ordre & dépendances

```
[ V1 ]
  └→ Finir 1.3 (PDF) + 1.4 (profil)
        │
        └→ V2.0 Conversations  ──→  V2.1 Rêveries  ──→  V2.2 Transformation
                                                              └→ V2.3 Polish
```

V2.1 dépend de V2.0 ? **Non techniquement**, mais le brief impose l'ordre §19. On respecte.

---

## 8 · Pour Arthur — décision attendue

Trois scénarios possibles, à trancher :

### Scénario A — On finit V1 d'abord (recommandé brief §0)
1. Patch v1.3 (aperçu + PDF)
2. Patch v1.4 (profil)
3. Validation V1 par Laurie
4. Démarrage V2.0

### Scénario B — On fait V1.4 (profil) seulement, on saute le PDF
Le PDF est la « promesse mémorisable » du brief V1. Le sauter affaiblit la valeur produit. Possible mais déconseillé.

### Scénario C — On bascule V2 maintenant (à tes risques)
On laisse v1.3 et v1.4 en suspens. La V1 reste « inachevée » mais l'utilisateur peut commencer à utiliser Histoire et Rêveries. Risque : feature creep, jamais de PDF livré.

→ **Recommandation Claude Code : Scénario A**.

Voir `v2-questions.md` pour les 8 questions à clarifier avant de démarrer V2.0.
