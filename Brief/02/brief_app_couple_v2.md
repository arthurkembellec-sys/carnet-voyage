# Addendum au brief — Histoire, Conversations, Rêveries

> Document complémentaire au brief principal `brief_app_couple.md`.
> Auteure : Laurie · Date : mai 2026 · Version : 1.1
> **À intégrer après livraison V1.** Ces ajouts sont V2.

---

## Préambule

Ce document complète le brief principal. Il introduit trois concepts produit qui n'étaient pas dans la V1 mais qui sont essentiels à la vision de l'app :

1. **L'historique du couple** — l'archive de la conversation d'origine (Hinge) intégrée à l'app comme point zéro de l'histoire
2. **La conversation continue** — un fil de messagerie interne au couple, dans la continuité directe de l'archive
3. **Les carnets de rêveries** — un type de carnet incubateur d'idées, qui sert de matrice à un futur carnet de voyage

**Position dans la roadmap :** ces fonctionnalités constituent la V2. Ne pas les implémenter avant que la V1 (création de carnet de voyage → album → impression) soit pleinement fonctionnelle et validée par les utilisateurs.

---

## 14 · Histoire & Conversations — le fil principal

### Concept produit

L'app a un *avant* et un *présent*. L'avant, c'est la conversation d'origine du couple (typiquement importée depuis Hinge ou une autre app de rencontre, mais le format reste générique). Le présent, c'est tout ce qui s'écrit ensuite, dans l'app.

Les deux vivent dans **un seul fil chronologique continu**, accessible depuis un onglet dédié de l'app. On scrolle, et on traverse l'histoire du couple : du tout premier message échangé jusqu'au dernier mot écrit hier soir.

### Tonalité

C'est un espace **intime et lent**. Pas une messagerie d'urgence (WhatsApp, iMessage existent pour ça). On y écrit des choses qu'on garde. Une réflexion après une expo, une photo prise dans le métro, une idée pour le week-end prochain.

> Pour reprendre l'esprit posé en section 1 : *« Le contraire d'une app saturée d'options. »* La conversation continue dans la même veine — éditoriale, posée.

### Architecture

#### Onglet « Histoire »

Nouvel onglet principal de l'app, à côté de « Carnets ». À voir avec ce qui est déjà en place : si l'app a une bottom bar, c'est un nouvel item ; si c'est une top bar, idem.

Composé de :

- **Section d'archive** (en haut, scroll initial) — la conversation importée, en lecture seule, présentée par chapitres comme dans la maquette web `notre_histoire.jsx`.
- **Section continue** (en dessous, à la suite) — les messages, photos et notes échangés dans l'app depuis sa création.

L'utilisateur scrolle naturellement de l'un à l'autre. Le passage entre archive et conversation continue est marqué par un séparateur visuel discret (filet horizontal + label « ICI COMMENCE NOTRE APP » ou similaire, à affiner).

#### Composer (zone de saisie)

En bas de l'écran, sticky :

- Champ texte (multi-lignes, expansible)
- Bouton **« + »** pour joindre :
  - Une photo (depuis pellicule)
  - Une photo (caméra live)
  - Un lien (URL collée → preview automatique)
  - Une référence à un carnet existant (mention `@`)
  - Une note vocale (V3, optionnel)

#### Affichage des messages

- Bulles de couleurs distinctes pour chaque membre du couple (réutiliser la palette du design system : noir profond pour l'un, terracotta pour l'autre, configurable dans le profil)
- Tap sur un message → menu contextuel : copier, modifier (si auteur), supprimer (si auteur), citer
- Heure affichée discrètement, regroupements par jour avec en-tête de date (« hier », « lundi 4 mai », etc.)
- Photos jointes affichées inline, plein largeur, tap pour viewer plein écran
- Liens externes : preview automatique (image OG, titre, source)
- Mentions de carnet : pastille cliquable qui ouvre le carnet

### Modèle de données

#### `Conversation`

Une seule conversation par couple. Elle contient à la fois l'archive importée et la conversation continue. Le champ `kind` permet de distinguer l'origine de chaque message.

| Champ | Type | Notes |
|---|---|---|
| `id` | UUID | PK |
| `coupleId` | UUID | FK, unique |
| `archiveImportedAt` | timestamp? | Quand l'archive a été importée |
| `archiveSource` | string? | Ex. « hinge », « tinder », « manual » |
| `createdAt` | timestamp | |

#### `Message`

| Champ | Type | Notes |
|---|---|---|
| `id` | UUID | PK |
| `conversationId` | UUID | FK |
| `kind` | enum | `archived` (importé, immuable), `live` (écrit dans l'app) |
| `chapterId` | UUID? | Si `archived` : référence au chapitre d'origine |
| `senderType` | enum | `userA`, `userB`, `system` |
| `senderId` | UUID? | FK User si `live`. Null si archive ou `system`. |
| `senderLabel` | string? | Pour archive : nom tel qu'écrit dans la conversation source |
| `body` | text | Contenu texte (markdown léger autorisé pour `live`) |
| `attachmentType` | enum? | `photo`, `link`, `voice`, `carnet_ref`, null |
| `attachmentRef` | string? | URL ou ID selon le type |
| `sentAt` | timestamp | Date du message d'origine pour archive, date d'envoi pour live |
| `editedAt` | timestamp? | Si modifié (uniquement `live`) |

#### `Chapter` (pour la section archive)

Permet de regrouper les messages archivés en chapitres lisibles, comme dans la maquette `notre_histoire.jsx`.

| Champ | Type | Notes |
|---|---|---|
| `id` | UUID | PK |
| `conversationId` | UUID | FK |
| `position` | int | Ordre dans l'archive |
| `title` | string | Ex. « Le like », « Mano Mano, Jules Joffrin » |
| `headline` | string? | Chapeau éditorial |
| `dateLabel` | string | Ex. « 31 août », « 4 — 5 septembre » |
| `weekdayLabel` | string? | Ex. « DIMANCHE », « JEUDI · VENDREDI » |
| `featuredImageUrl` | string? | Photo d'illustration du chapitre (ex. la photo Sicile pour le chapitre 1) |
| `imageCaption` | string? | |

### Comportements clés

#### Immuabilité de l'archive

- Les messages avec `kind = archived` ne peuvent **jamais** être modifiés ou supprimés.
- Les chapitres sont en lecture seule.
- Visuellement, l'archive a une présentation distincte (typographie plus serrée, en-têtes de chapitre, photo de couverture par chapitre).

#### Import de l'archive

- À l'onboarding du couple, proposer (optionnel) : « Avez-vous une conversation à importer ? »
- Format d'import attendu : JSON structuré (à définir précisément dans la doc), avec :
  - Liste des messages (auteur, date, contenu, attachements éventuels)
  - Métadonnées de chapitre si disponibles (titre, chapeau, photo de couverture)
- Si pas d'archive à importer : l'app fonctionne très bien sans. Le fil démarre directement en mode `live`.
- L'import est définitif (pas de modification possible après). On peut en revanche **réimporter** une archive si on remplace la précédente — confirmation forte requise.

#### Édition d'un message live

- Auteur uniquement
- Indicateur visuel discret « modifié » avec timestamp
- Historique des modifications V3, pas V2

#### Suppression d'un message live

- Auteur uniquement
- Soft delete : le message reste visible mais avec mention « Message supprimé »
- Définitif après 30 jours

#### Photos dans la conversation

- Compression côté client (mêmes règles que pour les carnets, cf. brief principal § 8)
- Affichage inline, ratio préservé
- Stockage cloud, scopé au couple
- **Important** : ces photos sont distinctes des photos de carnets. Mais l'utilisateur peut les ajouter à un carnet via une action « Ajouter à un carnet » dans le menu contextuel d'un message photo.

---

## 15 · Les carnets de rêveries

### Concept produit

Un carnet de rêverie, c'est l'**incubateur d'un futur voyage** (ou simplement d'une idée qu'on aime tourner sans urgence). On y dépose des items au fur et à mesure : une photo Instagram qui donne envie, un lien Maps vers un restaurant repéré, une note manuscrite, un nom de quartier, un budget approximatif.

C'est libre, mouvant, jamais figé. **Une rêverie n'est jamais imprimée.** Elle existe pour préparer, pas pour conserver.

> Exemples typiques : « Sicile en septembre », « Lisbonne un de ces jours », « Lune de miel », « Week-end Pyrénées », « Restos à tester à Paris ».

### Architecture

#### Position dans l'app

Les rêveries vivent **à côté** des carnets de voyage / restaurant / sortie, dans la même section « Carnets ». Mais elles sont distinguées :

- Filtre dédié dans la barre de filtres : « Voyages · Restaurants · Sorties · **Rêveries** · Toutes »
- Visuellement : aspect « brouillon » assumé. Bordure pointillée sur les cartes de la liste, fond légèrement teinté ou texturé (à affiner avec le designer).
- Icône distinctive (idée : nuage léger, étoile ténue, ou simple traitement typographique en italique).

#### Création

Bouton « + Nouvelle rêverie » disponible dans la liste des rêveries (et dans le menu de création global de l'app).

Formulaire minimal — *moins* contraint que la création d'un carnet de voyage :

- Titre (required, ex. « Sicile septembre »)
- Description courte (optionnel, 1-2 lignes)
- Photo de couverture (optionnel)

Pas de date imposée, pas de lieu structuré. Tout reste léger.

#### Items d'une rêverie

Une rêverie est une **liste d'items** très flexibles. Chaque item peut être :

- **Lien** (URL : Maps, Instagram, article, autre) avec preview automatique
- **Photo** (depuis pellicule ou caméra)
- **Note** (texte libre, markdown léger autorisé)
- **Lieu** (titre + adresse / coordonnées si possible)
- **Budget** (montant + devise + commentaire)

Affichage : grille responsive, chaque item dans une carte, ordre libre (drag & drop pour réorganiser).

Pas de section, pas de chapitre, pas de structure imposée. Une rêverie ressemble à un moodboard textuel.

### Modèle de données

#### `Reverie`

| Champ | Type | Notes |
|---|---|---|
| `id` | UUID | PK |
| `coupleId` | UUID | FK |
| `title` | string | Required |
| `description` | string? | |
| `coverPhotoId` | UUID? | FK Photo |
| `status` | enum | `dreaming` (en cours), `transformed` (a donné lieu à un voyage), `completed` (le voyage issu est imprimé), `archived` (abandonnée) |
| `transformedIntoCarnetId` | UUID? | FK Carnet, si `transformed` ou `completed` |
| `createdBy` | UUID | FK User |
| `createdAt` | timestamp | |
| `updatedAt` | timestamp | |

#### `ReverieItem`

| Champ | Type | Notes |
|---|---|---|
| `id` | UUID | PK |
| `reverieId` | UUID? | FK Reverie. **Nullable** : peut être null si l'item a été déplacé vers un voyage (cf. § 16). |
| `targetCarnetId` | UUID? | FK Carnet. Si non-null, l'item a été déplacé vers ce voyage et n'apparaît plus dans la rêverie. |
| `position` | int | Ordre dans la rêverie ou le voyage |
| `kind` | enum | `link`, `photo`, `note`, `location`, `budget` |
| `title` | string? | Titre court de l'item |
| `body` | text? | Contenu (note, description) |
| `url` | string? | Si `kind = link` ou `kind = location` |
| `photoId` | UUID? | Si `kind = photo` |
| `address` | string? | Si `kind = location` |
| `geoLat` | float? | Si `kind = location` et coordonnées disponibles |
| `geoLng` | float? | |
| `amount` | decimal? | Si `kind = budget` |
| `currency` | string? | Si `kind = budget`, ex. « EUR » |
| `addedBy` | UUID | FK User |
| `addedAt` | timestamp | |

> **Précision sur l'invariant d'unicité** : un `ReverieItem` est rattaché soit à une `reverieId` (et `targetCarnetId` est null), soit à un `targetCarnetId` (et `reverieId` est null). Jamais aux deux. Jamais à aucun. Cette contrainte est essentielle au modèle « items déplacés » (cf. § 16).

### Cycle de vie d'une rêverie

```
created (status: dreaming)
  │
  ├──→ user creates voyage from this dream
  │     status: transformed
  │     transformedIntoCarnetId: <uuid>
  │     items moved: ReverieItem.reverieId = null,
  │                  ReverieItem.targetCarnetId = <uuid>
  │
  ├──→ voyage is locked (printed)
  │     status: completed
  │
  └──→ user archives manually
        status: archived
```

---

## 16 · Du rêve au voyage — la transformation

### Flux utilisateur

#### Étape 1 — Action « Créer un voyage »

Depuis une rêverie ouverte, action principale en pied d'écran ou dans le menu :

> **« Transformer en voyage »**

#### Étape 2 — Choix des items à emporter

Écran intermédiaire :

- Liste des items de la rêverie, chacun avec une **case à cocher**
- En haut : toggle « Tout sélectionner / Tout désélectionner »
- En bas : compteur « 7 items sélectionnés sur 12 »
- Bouton **« Continuer »**

L'utilisateur sélectionne quels items il veut **emporter** dans le voyage. Les items non sélectionnés restent dans la rêverie (qui peut donc rester active pour un futur voyage).

#### Étape 3 — Fiche du voyage

L'utilisateur arrive sur la fiche de création de carnet de voyage standard (cf. brief principal § 4 étape 1) :

- Titre — pré-rempli avec le titre de la rêverie, modifiable
- Type — pré-rempli sur `voyage`
- Lieu, dates — vides ou pré-remplis selon ce que la rêverie contient
- Photo de couverture — pré-remplie avec celle de la rêverie si disponible

L'utilisateur valide la fiche.

#### Étape 4 — Le voyage est créé

- Carnet de voyage créé avec `parentReverieId` pointant vers la rêverie d'origine
- Les items sélectionnés sont **déplacés** vers le voyage : leur `reverieId` devient null, leur `targetCarnetId` devient l'ID du nouveau carnet
- La rêverie passe en statut `transformed`
- L'utilisateur arrive directement en mode édition album du nouveau voyage

### Comportements clés

#### Items déplacés, pas copiés

- Un item disparaît définitivement de la rêverie au moment du transfert.
- C'est volontaire : la rêverie représente le rêve, le voyage représente la réalisation. Une fois qu'un item passe « du rêve à la réalité », il appartient au réel.
- Conséquence : si l'utilisateur veut garder une trace dans la rêverie ET avoir l'item dans le voyage, il doit duppliquer manuellement avant. **Proposer une option** dans l'écran de sélection : « Dupliquer plutôt que déplacer » (case à cocher globale, off par défaut).

#### Une rêverie peut donner plusieurs voyages

- Statut `transformed` n'est pas terminal — il indique simplement qu'au moins un voyage en a été extrait.
- L'utilisateur peut continuer à enrichir la rêverie après une première transformation, et créer un autre voyage plus tard.
- Tant que la rêverie a encore des items, elle reste active.

#### Lien parent-enfant maintenu

- Sur la fiche d'un carnet de voyage : badge discret « ✨ né d'une rêverie » avec tap pour aller voir la rêverie d'origine.
- Sur la fiche d'une rêverie transformée : section « Voyages issus de cette rêverie » avec liste des voyages créés.

#### Fin de cycle : voyage imprimé → rêverie complétée

Quand le carnet de voyage passe en statut `locked` (cf. brief principal § 4 étape 4), si la rêverie d'origine n'a **plus aucun item restant** :

- La rêverie passe automatiquement en statut `completed`
- Elle est archivée dans une section « Rêveries réalisées »
- Plus modifiable, mais consultable

Si la rêverie a encore des items, elle reste active, indépendamment de l'état du voyage.

#### Que se passe-t-il si on supprime la rêverie d'origine ?

- Confirmation forte requise
- Le voyage issu est conservé (le lien `parentReverieId` devient pointe vers null avec mention « rêverie supprimée »)
- Rien n'est cassé côté voyage

#### Que se passe-t-il si on supprime le voyage issu ?

- Confirmation forte
- Les items déplacés ne reviennent **pas** automatiquement dans la rêverie (sinon on perd le travail fait dans le voyage)
- À la place, proposer dans la confirmation : « Renvoyer les items vers la rêverie d'origine ? »

---

## 17 · Mise à jour du modèle de données

Récapitulatif des modifications à apporter au modèle de données du brief principal (§ 3) :

### Modifications de tables existantes

#### `Carnet`

Ajouter :

| Champ | Type | Notes |
|---|---|---|
| `parentReverieId` | UUID? | FK Reverie, si le carnet a été créé depuis une rêverie |

Modifier l'enum `type` :

```
type: voyage | restaurant | sortie | reverie | autre
```

> Note : `reverie` est ici uniquement pour cohérence d'enum. En pratique, les rêveries sont stockées dans la table `Reverie` séparée, pas dans `Carnet`. Le choix entre tout fusionner dans `Carnet` (avec un champ `type`) ou séparer dans deux tables (`Carnet` et `Reverie`) est laissé à Claude Code, après inspection de l'existant. Recommandation par défaut : **deux tables séparées**, parce que les rêveries ont des items très différents des albums photos.

### Nouvelles tables

- `Conversation` (cf. § 14)
- `Message` (cf. § 14)
- `Chapter` (cf. § 14)
- `Reverie` (cf. § 15)
- `ReverieItem` (cf. § 15)

### Migration

- Tous les couples existants à la mise en V2 doivent automatiquement avoir une `Conversation` créée avec `archiveImportedAt = null`, prête à recevoir des messages live.
- Aucune donnée existante n'est cassée. La V2 est purement additive.

---

## 18 · Écrans à ajouter / modifier

### Nouveaux écrans

#### G. Onglet Histoire (§ 14)

- Vue principale : fil chronologique unifié archive + live
- Composer en bas
- Tap sur photo : viewer plein écran (réutiliser le composant V1)
- Tap sur lien : preview puis ouverture externe

#### H. Liste des rêveries (§ 15)

- Filtrable depuis l'onglet Carnets, ou onglet dédié si la stack le permet sans surcharger
- Cartes au style « brouillon »
- Tri : récentes en haut

#### I. Détail d'une rêverie (§ 15)

- Header avec titre, description, photo de couverture
- Grille d'items (drag & drop pour réordonner)
- Boutons d'ajout d'items (par type)
- Action principale en bas : « Transformer en voyage »

#### J. Sélection d'items pour transformation (§ 16)

- Liste à cocher des items
- Toggle global
- Compteur de sélection
- Toggle « Dupliquer plutôt que déplacer »
- Bouton « Continuer »

#### K. Vue « Rêveries réalisées »

- Accessible depuis un sous-menu de l'onglet Carnets, ou des paramètres
- Liste en lecture seule des rêveries en statut `completed`
- Tap sur une rêverie : vue figée avec lien vers le voyage qui l'a réalisée

### Écrans modifiés

#### Accueil / Carnets

- Ajout du filtre « Rêveries »
- Ajout d'un toggle ou d'un sous-onglet pour basculer rapidement Voyages / Rêveries

#### Fiche carnet de voyage

- Si `parentReverieId` non null : badge « ✨ née d'une rêverie » cliquable
- Section « Items hérités de la rêverie » au-dessus des photos du voyage, dépliable

#### Profil

- Indication du nombre de rêveries actives, voyages, et messages échangés
- Bouton « Importer une archive de conversation » (si pas encore fait à l'onboarding)

---

## 19 · Priorités V2

Ordre d'implémentation recommandé après V1 livrée et stabilisée :

### V2.0 — Conversations

1. Modèle `Conversation` + `Message` + `Chapter`
2. Onglet Histoire avec composer simple (texte uniquement)
3. Affichage des messages live
4. Import d'archive (JSON) avec interface d'admin minimale
5. Affichage de l'archive en chapitres
6. Pièces jointes : photos, liens

### V2.1 — Rêveries

1. Modèle `Reverie` + `ReverieItem`
2. Création d'une rêverie
3. Ajout d'items (link, note, photo)
4. Détail d'une rêverie avec grille d'items
5. Réorganisation par drag & drop
6. Items `location` et `budget`

### V2.2 — Transformation rêverie → voyage

1. Action « Transformer en voyage »
2. Sélection d'items
3. Pré-remplissage de la fiche du voyage
4. Migration des items vers le voyage
5. Lien parent-enfant maintenu et affiché
6. Cycle de vie automatique (rêverie completed quand voyage locked)

### V2.3 — Polish

- Référencer un carnet dans un message (mention `@`)
- Notes vocales dans les messages
- Filtres et recherche dans la conversation
- Statistiques sur le profil

---

## 20 · Hors-scope V2 (à reporter)

- ❌ Conversation chiffrée bout-en-bout (privacy par scope cloud suffit pour V2)
- ❌ Messages programmés / différés
- ❌ Réactions emoji sur les messages
- ❌ Réponses imbriquées (threads)
- ❌ Recherche full-text dans les conversations (V3)
- ❌ Export de la conversation au format livre imprimable (V3, mais clairement à envisager — c'est cohérent avec le produit)
- ❌ Synchronisation avec une app tierce de voyage (TripIt, Google Trips…)
- ❌ Suggestions IA pour transformer une rêverie en voyage

---

## 21 · Méthodologie spécifique V2

En plus des règles du brief principal § 11 :

1. **L'archive Hinge importée doit être traitée comme la donnée la plus précieuse** de l'app. Aucune feature ne doit pouvoir l'altérer. Tests dédiés : vérifier qu'aucune route, aucun composant, ne permet d'écrire dessus.
2. **La transformation rêverie → voyage est une opération atomique** : soit tout réussit (items déplacés, voyage créé, rêverie mise à jour), soit rien ne change. Implémenter en transaction si la base le permet.
3. **Séparer clairement les concepts** dans le code : `Conversation` n'est pas un `Carnet`, une `Reverie` n'est pas un `Carnet`. Même si le terme « carnet » est utilisé dans le langage de l'app, les modèles doivent rester distincts.
4. **Préserver la cohérence visuelle** avec la V1. Les conversations, rêveries et voyages doivent se sentir cousus dans le même tissu — même typo, mêmes couleurs, mêmes espacements. Ne pas céder à la tentation de différencier visuellement à outrance.

---

## 22 · Format d'import attendu pour l'archive

Pour la fonction d'import de l'archive (§ 14), Claude Code définira un format JSON. Voici une **proposition de structure** à valider avant implémentation :

```json
{
  "source": "hinge",
  "importedAt": "2026-05-04T10:30:00Z",
  "participants": [
    { "label": "Arthur", "userBinding": "userA" },
    { "label": "Laurie", "userBinding": "userB" }
  ],
  "chapters": [
    {
      "position": 1,
      "title": "Le like",
      "headline": "Une photo en Sicile. Un swipe à droite. Puis trente-deux heures de silence.",
      "dateLabel": "31 août",
      "weekdayLabel": "DIMANCHE",
      "featuredImageUrl": "https://...",
      "imageCaption": "« Tu n'aimes pas la montagne » · Isola Bella, Taormina",
      "messages": [
        {
          "senderType": "system",
          "body": "Arthur a liké ta photo",
          "sentAt": "2025-08-31T13:08:00Z"
        }
      ]
    },
    {
      "position": 2,
      "title": "Salut Arthur :)",
      "headline": "Le premier message tombe à 21h35…",
      "dateLabel": "1 septembre",
      "weekdayLabel": "LUNDI",
      "messages": [
        {
          "senderType": "userB",
          "body": "Salut Arthur :) tu vas bien ?",
          "sentAt": "2025-09-01T21:35:00Z"
        },
        {
          "senderType": "userA",
          "body": "Bonjour Laurie, très bien et toi ?",
          "sentAt": "2025-09-01T21:38:00Z"
        }
      ]
    }
  ]
}
```

Le mapping vers le modèle de données est direct.

---

## 23 · Définition de Done — V2

Une feature V2 est terminée quand, en plus des critères V1 (cf. brief principal § 9) :

- ✓ La V1 n'est pas régressée (tests V1 passent toujours)
- ✓ L'archive importée est strictement immuable (test dédié)
- ✓ La transformation rêverie → voyage est testée avec un cas nominal et au moins deux cas limites (rêverie vide, items déjà transformés)
- ✓ Le cycle de vie automatique (rêverie → completed) fonctionne avec et sans items résiduels
- ✓ Les permissions cloud isolent bien chaque couple (test : utilisateur du couple A ne peut pas accéder à un message du couple B)

---

## 24 · Pour Claude Code — instruction d'enchaînement

Quand la V1 est validée et qu'il est temps de démarrer la V2 :

1. Relire ce document **et** le brief principal
2. Produire `/docs/v2-plan.md` avec :
   - Récapitulatif des nouveaux modèles et écrans
   - Plan de migration des données existantes (devrait être trivial)
   - Liste des questions à clarifier avec moi
3. Attendre validation avant tout code

Comme pour la V1 : ne pas se disperser, suivre l'ordre de § 19, livrer V2.0 avant V2.1, etc.

---

**Fin de l'addendum.**
