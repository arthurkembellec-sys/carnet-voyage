# BRIEF — AUDIT DE REPRISE COMPLÈTE (UX mobile + Livre PDF)

> Établi le 2026-08-15 à la demande d'Arthur (« le user n'est pas suffisamment aidé ;
> les belles présentations ne sont pas exploitées, surtout le PDF qui est l'objectif final »),
> par deux audits professionnels indépendants menés sur le code réel :
> - **Annexe A** : `briefs/annexes/2026-08-15_AUDIT_UX_MOBILE.md` (lead product designer mobile)
> - **Annexe B** : `briefs/annexes/2026-08-15_AUDIT_LIVRE_PDF.md` (directeur artistique livres photo)
>
> Chaque constat ci-dessous est vérifié (fichier + ligne, reproduction sur la base de test).
> Statuts : ✅ corrigé en v5.18 · 🔲 chantier à valider par Arthur avant exécution.

## 1. Le diagnostic en une phrase

L'app accumule des mécanismes excellents (carte, planning-perles, corbeilles, chronologie)
mais **chaque écran réinvente sa présentation au lieu de prolonger celle déjà apprise**,
personne ne dit à l'utilisateur *où faire quoi*, et **le livre — l'objectif final — perd
en route une partie de ce que l'app sait** (commentaires, ordre, carte, planning).

## 2. Corrigé immédiatement (v5.18 — correctifs de confiance)

1. ✅ **Chronologie du livre** (verbatim : « les photos datées par user apparaissent à la fin »).
   Cause CONFIRMÉE : `sort_mode='manual'` figé par d'anciens drags (carnet 1 l'était en base) ;
   les positions figées ignoraient les re-datations v5.13, et fabriquaient de faux bandeaux
   de jour. Correctif : migration `sort_mode='chrono'`, l'ancienne route de reorder ne bascule
   plus en manual, tri chronologique inconditionnel. La chronologie est désormais l'unique
   ordre de vérité, à l'écran comme au livre.
2. ✅ **« 4 portraits superposés à la verticale »** dans le PDF. REPRODUIT : le filtre de
   hauteur du calepinage v5.17 testait AVANT mise à l'échelle → toutes les compositions
   rejetées → secours littéralement « une colonne ». Correctif : score en AIRE couverte,
   filtre après échelle, secours en grille équilibrée, plancher 30 mm par rangée,
   anti-colonne. Validé sur 3 formats × 6 scénarios.
3. ✅ **Croix du planning inaccessibles en portrait**. Cause prouvée au pixel : l'en-tête
   des blocs exige ~292 px pour ~264 disponibles et `overflow:hidden` COUPE le ✕ ;
   plus `.day-step-del` absolu 26 px (< 44 px, règle D3). Correctif : retour dans le flux
   (flex), cibles 44 px, passage à la ligne en portrait.
4. ✅ **Renommer une épingle** (verbatim : « je ne peux changer de nom »). L'API n'existait
   pas. Ajout : `POST /item/<id>/titre` + champ de renommage dans les popups de carte
   (rêverie ET album), enregistré par le bouton OK existant.
5. ✅ **Écrasement silencieux du planning riche** (P0 de l'audit UX) : un simple drag dans
   le planning de l'album convertissait les blocs de la rêverie (modes 🚗🚶🚲, heures,
   haltes, « pas fait ») en blocs plats `voiture/sans heure`. Correctif : la sauvegarde
   depuis l'album FUSIONNE (conserve la structure des blocs existants, n'applique que
   les changements d'appartenance aux jours).
6. ✅ **Couverture : dates en français** (« 11 – 13 juillet 2026 » au lieu de l'ISO brut)
   et **fond perdu permanent** (gabarit constant pour l'imprimeur).

## 3. Chantiers à valider (ordre proposé)

### Chantier A+B — LA CARTE DU LIVRE + TIMELINE (réponse au verbatim carte)
La carte du PDF devient celle de l'app : tuiles CARTO voyager @2x (300 dpi), épingles-vignettes
photo rondes, pastilles d'étapes couleur par type + emoji, étiquettes de villes avec halo et
anti-collision, tracé du parcours jour par jour (plein le jour, pointillé entre jours), badges
de numéros de jour — et une **colonne timeline du planning** (jours + étapes numérotées comme
sur la carte) à gauche ou à droite au **choix de l'utilisateur** (`pdf_map_timeline_side`).
Spécification Pillow complète en Annexe B §4. **~2-3 jours.**

### Chantier C — LES COMMENTAIRES IMPRIMÉS (réponse au verbatim n° 1)
Les épingles `photo_notes` ne sont AUJOURD'HUI PAS DU TOUT branchées au moteur PDF (confirmé).
Convention du métier : **appels de note** ①②③ discrets sur la photo + texte attribué en marge
(« ① Laurie — “On y retourne ?” »). Au passage, 5 fuites de légendes colmatées (caption
perdue si la marge est pleine, spread sans légende, vidéo tronquée, troncatures muettes).
**~1-2 jours.**

### Chantier D — L'ARCHITECTURE DU LIVRE (ce qui fera « vrai livre »)
Page de garde + page de titre (au lieu de 2 pages blanches), **ouverture de chapitre par
jour** (pleine page : JOUR 3, date en toutes lettres, lieu, meilleure photo), **page de
statistiques du voyage** (jours, photos, lieux, km parcourus), 4e de couverture, pagination
belle-page. **~2 jours.**

### Chantier E — L'UTILISATEUR AIDÉ (réponse au « pas suffisamment aidé »)
- **Une mission par écran**, affichée : Rêverie = « on imagine », Album = « on raconte le
  vécu », Aperçu = « on met en page le livre ». Bandeau première-visite par écran +
  « ? » dans la topbar pour le rouvrir + hints permanents sur les zones muettes.
  Microcopies françaises déjà rédigées (Annexe A §4).
- **Un seul langage visuel pour le planning** (les perles apprises en rêverie deviennent
  la tête du planning album) + lien croisé permanent album ↔ rêverie d'origine.
- **Épingle de note photo intuitive** : appui long sur la photo → épingle posée + feuille
  basse de saisie (le geste standard), au lieu du bouton-mode actuel. **~2-3 jours.**

## 4. Ordre d'exécution proposé

v5.18 (fait) → **C** (commentaires : verbatim n° 1, dépendance d'aucun autre) →
**A+B** (carte + timeline : le plus visible) → **D** (architecture) → **E** (aide + unification).
Chaque chantier = une session, batterie complète, PDF regardé page par page avant push.

## 5. Règle de conduite retenue (des deux auditeurs)

« Tant qu'un livre peut perdre des textes ou inverser des jours, aucune beauté ne
rattrape l'objet. » Les correctifs de confiance passent toujours avant l'esthétique.
