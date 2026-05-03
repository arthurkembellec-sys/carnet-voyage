# Brief produit & technique — App couple « Notre Histoire »

> Document destiné à **Claude Code**, à exécuter sur le projet existant (référence : `AQGK`).
> Auteure : Laurie · Date : mai 2026 · Version : 1.0

---

## 0 · Pré-flight — à faire avant d'écrire la moindre ligne

**Avant toute implémentation**, Claude Code doit :

1. **Inspecter le codebase existant** du projet `AQGK` :
   - Lister la structure des dossiers
   - Identifier le langage / framework / plateforme cible (web, iOS natif, React Native, Flutter, autre)
   - Lister les dépendances (`package.json`, `Podfile`, `requirements.txt`, `pubspec.yaml`, etc.)
   - Identifier la stratégie de stockage déjà en place (Firebase, Supabase, base locale, autre)
   - Identifier le système d'authentification existant
   - Lire le `README` et toute documentation présente
   - Identifier les conventions de code (linter, formatter, structure de dossiers, naming)
   - Identifier les composants UI déjà construits (design system, librairie de composants)

2. **Produire une note de synthèse** (`/docs/state-of-the-app.md`) qui répond à :
   - Quelle est la stack ?
   - Quelle est l'architecture actuelle ?
   - Qu'est-ce qui est déjà construit, qu'est-ce qui ne l'est pas ?
   - Quelles décisions structurelles ont déjà été prises ?
   - Quels écarts entre ce brief et l'existant ?

3. **Proposer un plan d'exécution** avant de coder :
   - Ordre des tâches V1
   - Dépendances entre tâches
   - Identifier les sujets ambigus à valider avec moi

**Règle absolue** : ne pas refaire ce qui existe déjà. Étendre, ne pas remplacer.

---

## 1 · Vision produit

**Le produit en une phrase :**
Une app pour deux. Un compte par couple. On y archive nos voyages, nos restaurants, nos sorties, en photos et mots, pour finir par les imprimer en livre.

**Pour qui :**
Un couple — deux personnes connectées au même compte, qui voient et éditent les mêmes contenus.

**Ce que ce n'est pas :**
- Pas un réseau social. Aucun feed public, aucun « like », aucun follower.
- Pas un cloud photo généraliste. C'est curé, intentionnel, éditorial.
- Pas un Instagram pour couples. C'est plus lent, plus posé, et ça finit en papier.

**Tonalité :**
Privée. Élégante. Simple. Le contraire d'une app saturée d'options. Chaque écran fait une chose. Les photos sont grandes. Le texte respire.

**Promesse mémorisable :**
> Ouvrir l'app un dimanche soir. Faire défiler les photos du week-end. Écrire trois lignes. Et, dans six mois, recevoir le livre par la poste.

---

## 2 · Architecture du compte couple

C'est le concept central de l'app. À implémenter avec soin.

### Principe

- **Un compte = un couple** (= une entité partagée entre deux utilisateurs)
- Chaque utilisateur a son identité (nom, photo de profil, email)
- Mais **toutes les données de contenu** (carnets, albums, photos, commentaires) appartiennent au **couple**, pas à un utilisateur individuel.

### Onboarding

**Premier utilisateur (créateur) :**
1. S'inscrit (email + mot de passe, ou magic link, ou Apple/Google selon ce qui est déjà en place)
2. Crée le couple : « Comment voulez-vous appeler votre compte ? » (ex. *Arthur & Laurie*, ou rien — c'est optionnel)
3. Reçoit un **lien d'invitation** à envoyer à son ou sa partenaire (lien profond / deep link)

**Second utilisateur :**
1. Clique sur le lien d'invitation
2. S'inscrit également
3. Est automatiquement rattaché au couple
4. Confirmation visuelle : « Vous êtes maintenant connectés à *X* »

### Permissions

- Les deux membres ont **des droits identiques** sur tous les contenus du couple. Pas de hiérarchie, pas d'admin.
- Quand un membre ajoute, modifie ou supprime quelque chose, l'autre voit le changement la prochaine fois qu'il rafraîchit (ou en temps réel si la stack le permet).
- **Tracer qui a fait quoi** discrètement : chaque photo, chaque commentaire, chaque carnet a un `addedBy: userId`. Affiché de façon subtile, jamais agressivement.

### Cas limites à gérer

- Un membre veut quitter le couple → confirmation, suppression de l'association, ses contributions restent (avec mention « ancien membre »)
- Suppression du compte → soft delete, fenêtre de récupération de 30 jours
- Aucun mode « solo » : si on est seul, le compte fonctionne quand même (en attendant le ou la partenaire)

---

## 3 · Modèle de données

À adapter selon la base existante (PostgreSQL si Supabase, Firestore si Firebase, etc.).

### `Couple`
| Champ | Type | Notes |
|---|---|---|
| `id` | UUID | PK |
| `name` | string? | Optionnel, ex. « Arthur & Laurie » |
| `createdAt` | timestamp | |
| `memberIds` | UUID[] | Max 2 |

### `User`
| Champ | Type | Notes |
|---|---|---|
| `id` | UUID | PK |
| `email` | string | Unique |
| `displayName` | string | Prénom |
| `avatarUrl` | string? | |
| `coupleId` | UUID? | Null tant que pas dans un couple |
| `createdAt` | timestamp | |

### `Carnet`
| Champ | Type | Notes |
|---|---|---|
| `id` | UUID | PK |
| `coupleId` | UUID | FK |
| `title` | string | Ex. « Week-end à Annecy » |
| `type` | enum | `voyage`, `restaurant`, `sortie`, `autre` |
| `location` | string? | Texte libre |
| `dateStart` | date? | |
| `dateEnd` | date? | |
| `coverPhotoId` | UUID? | FK Photo |
| `status` | enum | `draft` (en cours de création), `active` (en édition album), `locked` (envoyé à l'impression), `archived` |
| `createdBy` | UUID | FK User |
| `createdAt` | timestamp | |
| `updatedAt` | timestamp | |

### `AlbumPage`
Une page d'album = soit une photo, soit un bloc de texte. Ordre dans l'album = `position`.

| Champ | Type | Notes |
|---|---|---|
| `id` | UUID | PK |
| `carnetId` | UUID | FK |
| `type` | enum | `photo`, `text`, `spread` (double page) |
| `position` | int | Ordre dans l'album |
| `photoId` | UUID? | Si `type=photo` |
| `caption` | string? | Légende sous photo |
| `textContent` | string? | Si `type=text` |
| `layout` | enum | `full`, `half`, `third`, `grid2x2`, etc. (V2) |
| `createdAt` | timestamp | |

### `Photo`
| Champ | Type | Notes |
|---|---|---|
| `id` | UUID | PK |
| `coupleId` | UUID | FK (toutes les photos sont scopées au couple) |
| `originalUrl` | string | URL stockage cloud (haute résolution) |
| `thumbnailUrl` | string | URL stockage cloud (basse résolution) |
| `width` | int | |
| `height` | int | |
| `takenAt` | timestamp? | EXIF si dispo |
| `location` | geo? | EXIF si dispo |
| `addedBy` | UUID | FK User |
| `addedAt` | timestamp | |

### `Comment`
| Champ | Type | Notes |
|---|---|---|
| `id` | UUID | PK |
| `targetType` | enum | `photo`, `albumPage`, `carnet` |
| `targetId` | UUID | |
| `userId` | UUID | FK |
| `text` | string | |
| `createdAt` | timestamp | |

### `PrintOrder`
| Champ | Type | Notes |
|---|---|---|
| `id` | UUID | PK |
| `carnetId` | UUID | FK |
| `coupleId` | UUID | FK |
| `format` | enum | `square_20`, `landscape_a4`, `portrait_a5`, etc. |
| `pdfUrl` | string | Stockage cloud du PDF généré |
| `status` | enum | `pdf_generated`, `sent_to_provider`, `ordered`, `shipped`, `delivered` |
| `externalProvider` | string? | Nom du fournisseur (CEWE, Photoweb…) |
| `externalRef` | string? | Numéro de commande externe |
| `createdAt` | timestamp | |

---

## 4 · Flux principal — Création d'un album

C'est le cœur du produit. À implémenter en priorité absolue.

### Étape 1 — Créer le carnet (formulaire)

**Écran : « Nouveau carnet »**
- Titre (required, max 80 caractères)
- Type (segmented control : Voyage · Restaurant · Sortie · Autre)
- Lieu (texte libre, optionnel)
- Date début / date fin (optionnel — sélecteur de plage)
- Photo de couverture (optionnel à cette étape)
- Bouton **« Créer le carnet »** → status: `draft` → status: `active` après validation

> **Important** : à cette étape on **valide la fiche** du carnet avant de pouvoir y ajouter des photos. C'est intentionnel — la friction est utile, ça oblige à nommer le projet.

### Étape 2 — Mode album (édition)

**Écran : carnet en mode édition**

Composé de :
- **Header** sticky : titre du carnet, dates, bouton retour, menu (renommer, supprimer, partager preview)
- **Liste verticale des pages** : chaque page = photo + caption ou bloc texte
- **Footer flottant** : actions « + Photo » (depuis pellicule), « 📷 Caméra » (prendre photo), « ¶ Texte » (ajouter bloc texte), « 👁 Aperçu livre »

**Comportement « + Photo » :**
- Ouvre le sélecteur natif de photos
- Multi-sélection autorisée (jusqu'à 30 photos d'un coup)
- À l'ajout : compression côté client avant upload (max 2000px sur le côté long, qualité 85%)
- Upload en arrière-plan, indicateur de progression discret
- La photo apparaît en bas de l'album dès l'upload terminé
- Caption pré-remplie vide, prête à l'édition

**Comportement « 📷 Caméra » :**
- Demande la permission caméra si pas encore donnée
- Ouvre la caméra native
- Après capture, propose : « Ajouter au carnet » ou « Reprendre »
- Même flux upload que ci-dessus

**Comportement « ¶ Texte » :**
- Insère un bloc de texte vide en bas de l'album
- Édition inline (tap pour entrer en édition, tap ailleurs pour sortir)
- Markdown léger autorisé : **gras**, *italique*, retours ligne

**Édition d'une photo dans l'album :**
- Tap sur la photo → ouvre le viewer plein écran avec :
  - Photo en grand (pinch to zoom)
  - Caption éditable en dessous
  - Métadonnées (date, lieu si EXIF)
  - Commentaires des deux partenaires (V2)
  - Bouton supprimer (avec confirmation)
  - Swipe horizontal pour passer à la photo suivante / précédente

**Réorganisation :**
- Long-press sur une photo → mode drag pour réorganiser (V2 si compliqué)

**Auto-save :**
- Tout est sauvegardé en continu (chaque caption, chaque ajout). Pas de bouton « Enregistrer ».
- Indicateur discret « ✓ Enregistré » qui disparaît après 1s.

### Étape 3 — Aperçu livre

**Écran : aperçu du livre**

- Mise en page automatique style livre photo (paginé)
- Format par défaut : carré 20×20 cm
- Sélecteur de format : carré, paysage A4, portrait A5
- Navigation page par page (gauche/droite)
- Bouton **« Modifier »** retourne au mode édition album
- Bouton **« Commander en livre »** → étape 4

### Étape 4 — Impression (générique pour V1)

**Écran : commander le livre**

> **Note importante pour V1** : l'intégration directe avec un service d'impression (CEWE, Photoweb…) sera décidée plus tard. Pour V1, on génère un **PDF haute résolution** que l'utilisateur télécharge ou envoie par mail, et qu'il uploade ensuite manuellement chez l'imprimeur de son choix.

V1 :
- Génération du PDF côté serveur (haute résolution, marges respectées selon format)
- Bouton « Télécharger le PDF »
- Bouton « M'envoyer le PDF par email »
- À ce moment, status du carnet → `locked`
- Le carnet reste éditable (passage à `active` possible avec confirmation)

V2 (plus tard) :
- Intégration directe avec une API d'impression
- Choix du fournisseur dans les paramètres
- Suivi de commande dans l'app

---

## 5 · Écrans complets de l'app

### A. Onboarding
1. Écran de bienvenue
2. Inscription / connexion
3. Création du couple OU acceptation d'invitation
4. (Si création) Génération du lien d'invitation à partager

### B. Accueil — « Notre histoire »
- Liste verticale des carnets (les plus récents en premier)
- Chaque carnet : photo de couverture, titre, dates, type (badge), nb de photos
- Tap sur un carnet → ouvre soit la fiche (si `draft`) soit l'album (si `active`/`locked`)
- Bouton flottant « + Nouveau carnet »
- Filtre par type en haut (chips horizontales)

### C. Mode édition album
Voir étape 2 ci-dessus.

### D. Viewer photo plein écran
Voir étape 2.

### E. Aperçu livre
Voir étape 3.

### F. Profil / paramètres
- Photo et nom du couple
- Photo et nom de l'utilisateur courant
- Voir le ou la partenaire (avatar, nom, depuis quand)
- Notifications (V2)
- Préférences impression (V2)
- Se déconnecter
- Quitter le couple (confirmation forte)
- Supprimer le compte

---

## 6 · Design system

S'inspirer de — et idéalement **réutiliser** — le design system déjà posé dans la maquette web précédente du projet (fichier `notre_histoire.jsx` joint à ce brief, à transposer dans la stack cible).

### Couleurs

```
--bg:           #FAF8F4   (crème chaud, fond principal)
--bg-card:      #FFFFFF   (cartes, contenus)
--ink:          #1C1A17   (texte primaire, presque noir chaud)
--ink-soft:     #3D3A35   (texte secondaire)
--muted:        #6B6963   (texte tertiaire, métadonnées)
--whisper:      #A39C92   (placeholder, captions discrètes)
--accent:       #A8503D   (terracotta, à utiliser avec parcimonie)
--border:       rgba(28, 26, 23, 0.06)
--border-strong: rgba(28, 26, 23, 0.12)
```

### Typographie

- **Display / titres** : Fraunces (variable serif, axes opsz et SOFT)
  - Titres principaux : Fraunces italic, 300, opsz 144, SOFT 100
  - Titres secondaires : Fraunces, 400, opsz 144
- **Body / UI** : Geist (variable sans-serif)
  - Texte courant : Geist, 400
  - Métadonnées / labels : Geist, 500, lettrage espacé (letter-spacing 0.18em–0.32em), TOUT EN MAJUSCULES, taille 10–12px

> **Notes** : Fraunces et Geist sont sur Google Fonts. Si la stack ne permet pas Google Fonts (app native), embarquer les fichiers `.ttf` ou utiliser des équivalents système (San Francisco serif, etc.).

### Spacing

Échelle 4-8-12-16-20-24-32-40-60-80-120 px. Privilégier le whitespace généreux. Pas de padding inférieur à 12px sauf cas exceptionnel.

### Composants à construire

- `Button` (primary noir, secondary outline, ghost)
- `Input` (text, textarea, date, segmented)
- `Card` (élévation subtile, fond blanc, bordure ténue)
- `Modal` (depuis le bas sur mobile, centré sur desktop)
- `Tabs` (sticky top, pill style)
- `Chip` (pour filtres et types de carnet)
- `Avatar` (rond, initiales si pas d'image)
- `EmptyState` (illustration optionnelle + texte serif italique + CTA)
- `PhotoGrid` (responsive, lazy-loading)
- `PhotoViewer` (plein écran, swipe, pinch-zoom)

### Mobile-first

L'app est principalement consultée et éditée sur téléphone. Tous les écrans doivent fonctionner parfaitement à partir de 375px de large. Le desktop est une projection responsive du mobile, pas l'inverse.

---

## 7 · Priorités

### V1 — Must-have (objectif : tester le flux principal)

1. ✅ Auth + onboarding couple complet (création + invitation + association)
2. ✅ CRUD carnets (créer, lister, éditer la fiche, supprimer)
3. ✅ Mode édition album : ajout photo (pellicule + caméra), caption, suppression
4. ✅ Stockage photos + thumbnails (cloud)
5. ✅ Synchronisation entre les deux membres du couple
6. ✅ Aperçu livre basique (paginé, format carré par défaut)
7. ✅ Export PDF haute résolution
8. ✅ Profil minimal

### V2 — Nice-to-have

- Commentaires entre partenaires sur photos / pages
- Réorganisation par drag & drop
- Mise en page avancée (1, 2, 4 photos par page, doubles pages)
- Bloc texte entre photos avec markdown léger
- Plusieurs formats d'impression (paysage, portrait, A5, A4)
- Filtres et recherche dans les carnets
- Tags / lieux
- Notifications quand le ou la partenaire ajoute quelque chose
- Lien Instagram / Google Maps attaché à un carnet (cf. fonctionnalité « Carnets de liens » de la maquette précédente)

### V3 — Future

- Intégration API directe avec un imprimeur (CEWE / Photoweb / autre)
- Statistiques (X carnets, X photos, X livres imprimés)
- Mode « Histoire » pour archiver une conversation extérieure (genre l'archive Hinge — voir maquette précédente)
- Partage sélectif d'un carnet en lecture seule (lien public temporaire)
- Export vers cloud externe (Google Drive, Dropbox)
- Mode hors-ligne complet
- Apple Watch / widget iOS

---

## 8 · Considérations techniques

### Photos

- **Compression côté client avant upload** :
  - Max 2000 px sur le côté long
  - Qualité JPEG 85%
  - Conversion HEIC → JPEG si la plateforme le requiert
- **Génération thumbnail côté serveur ou client** : 400 px sur le côté long, qualité 70%
- **Lazy loading** dans la liste : ne charger que les thumbnails visibles + 1 écran de marge
- **Cache disque** des thumbnails sur le device

### Permissions natives

- iOS / Android : Camera, Photo Library
- Définir des messages clairs dans `Info.plist` / `AndroidManifest.xml`, ex. :
  > « Pour ajouter des photos à vos carnets de voyage. »

### Synchronisation

- Realtime si la stack le permet (Firestore listeners, Supabase realtime, etc.)
- Sinon : pull-to-refresh + refresh à l'ouverture
- Optimistic UI : afficher immédiatement, réconcilier avec le serveur en arrière-plan

### Auth

- Aligner sur ce qui existe dans le projet AQGK
- Si rien : recommander **magic link par email** (simple, sans gestion de mots de passe)
- Apple Sign-In requis si distribué sur l'App Store

### Stockage cloud

- Aligner sur ce qui existe (Firebase Storage, Supabase Storage, S3…)
- Règles de sécurité : un utilisateur ne peut accéder qu'aux fichiers de **son couple**

### Privacy / RGPD

- Tous les contenus sont privés au couple
- Aucun partage public par défaut
- Page « Politique de confidentialité » obligatoire
- Bouton « Exporter mes données » (V2) : zip de toutes les photos + JSON des métadonnées
- Bouton « Supprimer mon compte » : suppression complète après 30 jours

### Performance

- Time-to-interactive de l'écran d'accueil : < 1.5s sur connexion 4G correcte
- Lazy-load des carnets en scroll infini si > 30 carnets
- Preload de la photo suivante dans le viewer plein écran

### Tests

- Tests unitaires pour les utilitaires (compression, génération PDF, modèle de données)
- Tests d'intégration pour les flux principaux (création carnet, ajout photo, génération PDF)
- Pas de tests E2E pour V1 (overkill)

---

## 9 · Definition of Done — V1

Une feature est considérée terminée quand :

- ✓ Le code est mergé sur la branche principale
- ✓ Les tests unitaires passent (si pertinents)
- ✓ La feature fonctionne sur iPhone (réel ou simulateur récent)
- ✓ Les permissions natives sont demandées proprement
- ✓ Pas de warning console / linter
- ✓ Les états d'erreur sont gérés (pas de réseau, photo trop lourde, etc.)
- ✓ L'état de chargement est visible
- ✓ L'état vide a un design propre
- ✓ Le flux fonctionne avec deux comptes simultanés (vérifier la sync)
- ✓ Une démo vidéo de 30s du flux complet est fournie

---

## 10 · Hors-scope V1 (à dire non clairement)

- ❌ Paiement intégré pour l'impression (PDF généré + envoi externe pour V1)
- ❌ Notifications push
- ❌ Mode hors-ligne avancé (au-delà du cache simple)
- ❌ API impression directe
- ❌ Carnets publics, partage externe, réseau social
- ❌ IA / génération automatique de captions
- ❌ Reconnaissance faciale, géolocalisation avancée
- ❌ Versioning / historique des modifications
- ❌ Mode collaboratif live (deux personnes éditent le même album simultanément)
- ❌ Apple Watch, widgets iOS
- ❌ Marketplace de templates de mise en page

---

## 11 · Méthodologie de travail attendue de Claude Code

1. **Toujours commencer par lire l'existant** avant d'écrire du nouveau code
2. **Ne pas surinterpréter** : si une décision n'est pas claire dans ce brief, **poser la question** au lieu de deviner
3. **Petits commits fréquents** avec messages descriptifs
4. **Une feature à la fois**, dans l'ordre du V1 ci-dessus
5. **Tester chaque feature** sur device réel avant de passer à la suivante
6. **Documenter les choix non-évidents** dans `/docs/decisions/`
7. **Mettre à jour le `README`** au fur et à mesure
8. **Ne jamais casser une feature qui marche** — si une refacto est nécessaire, le dire et attendre validation
9. **Respecter le design system** du fichier `notre_histoire.jsx` — couleurs, typo, spacing
10. **Valider avec moi avant** :
    - Tout choix structurel (lib majeure, refonte, nouvelle dépendance lourde)
    - Tout abandon de fonctionnalité du V1
    - Toute modification du modèle de données après le premier setup

---

## 12 · Ressources fournies

- `notre_histoire.jsx` — maquette web React du design system et du flux « histoire / carnets ». À utiliser comme référence visuelle, pas comme code à porter directement.
- (À fournir si besoin) Conversation Hinge originale qui illustre la tonalité du produit.

---

## 13 · Premier livrable attendu

Avant tout code de feature :

1. `/docs/state-of-the-app.md` — la note de pré-flight (cf. section 0)
2. `/docs/v1-plan.md` — le plan d'exécution V1 avec ordre, dépendances, estimations
3. Une liste de **questions à clarifier** avec moi avant de démarrer

Ne pas commencer la V1 tant que ces trois livrables ne sont pas validés.

---

**Fin du brief.**
