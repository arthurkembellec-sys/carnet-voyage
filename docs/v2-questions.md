# Questions bloquantes V2 — « Notre Histoire »

> Demandé en §24 du brief V2.
> À répondre par Arthur AVANT de démarrer V2.0.

---

## A · Arbitrage roadmap

### A1. On finit V1 ou on bascule V2 maintenant ?
- Cf. `v2-plan.md` §8 — Scénarios A / B / C.
- **Recommandation Claude** : Scénario A (finir V1 = patches v1.3 PDF + v1.4 profil ~1.5j).
- **Réponse** : *(en attente)*

### A2. La V1 actuelle est-elle considérée comme « validée par les utilisateurs » ?
- Brief V2 §0 impose validation V1 par les vrais utilisateurs avant de démarrer V2.
- Toi + Laurie avez vraiment utilisé V1 (créé un carnet, ajouté des photos, écrit des captions) ?
- **Réponse** : *(en attente)*

---

## B · Conversations / Histoire

### B1. Couleur des bulles par membre du couple
- Brief §14 : « bulles de couleurs distinctes pour chaque membre — noir profond pour l'un, terracotta pour l'autre, configurable dans le profil ».
- Question : **configurable** = champ `color` sur `users` à ajouter ? Ou simplement « 1er user du couple = couleur 1, 2e user = couleur 2 » sans personnalisation V2 ?
- **Reco Claude** : V2.0 = pas configurable (couleur déterminée par ordre d'arrivée dans le couple), config dans V2.3 polish.
- **Réponse** : *(en attente)*

### B2. Où placer le bouton « Histoire » dans la nav ?
- Aujourd'hui topbar minimale : « ← Notre Histoire » (logo) à gauche, « Inviter / Déconnexion » à droite.
- Ajouter un onglet « Histoire » à droite du logo ? Ou bottom-bar mobile-first ?
- **Reco Claude** : nav haute reconfigurée en 2 onglets « Carnets · Histoire » centrés, profil/déconnexion en menu hamburger droit.
- **Réponse** : *(en attente)*

### B3. Import d'archive Hinge — qui fait l'extraction depuis Hinge ?
- L'app accepte du JSON au format §22 du brief. Mais qui produit ce JSON ?
- Hinge ne fournit pas d'export structuré standard. Options :
  - (a) Toi/Laurie copiez-collez manuellement dans le JSON
  - (b) Tu as un dump RGPD Hinge → on écrit un parser dédié
  - (c) On part sur un format générique manuel V2.0, parser Hinge en V2.0.1
- **Reco Claude** : (c) — page admin avec textarea, tu colles le JSON déjà au bon format. Parsing Hinge plus tard si dump RGPD obtenu.
- **Réponse** : *(en attente)*

### B4. Édition des messages live
- Brief §14 : « Édition d'un message live : auteur uniquement, indicateur visuel discret modifié ».
- Garder en V2.0, ou reporter en V2.0.1 ?
- **Reco Claude** : garder en V2.0 — feature simple si on a déjà le modèle.
- **Réponse** : *(en attente)*

---

## C · Rêveries

### C1. Item `location` — comment l'utilisateur entre les coordonnées ?
- Brief §15 : `kind: location` avec `address`, `geoLat`, `geoLng`.
- Saisie manuelle adresse OK. Mais geoLat/geoLng ?
- Options :
  - (a) Coller un lien Google Maps → on parse les coords V2.0
  - (b) Pas de coords V2.1, juste l'adresse texte
  - (c) Intégration carte (Leaflet) V2.2
- **Reco Claude** : (b) V2.1, (a) V2.1.1.
- **Réponse** : *(en attente)*

### C2. Item `budget` — devise
- Liste des devises ? Default EUR ?
- **Reco Claude** : champ texte libre 3 caractères, default `EUR`. Pas de validation stricte.
- **Réponse** : *(en attente)*

### C3. Style « brouillon » — jusqu'où ?
- Brief §15 : « bordure pointillée, fond légèrement teinté ou texturé ».
- Texture = trame papier kraft ? Cross-hatch CSS ? Juste une teinte différente ?
- **Reco Claude** : bordure pointillée + fond `#F5F0E6` (légèrement plus chaud que `--bg`). Pas de texture pour rester épuré.
- **Réponse** : *(en attente)*

---

## D · Transformation

### D1. « Dupliquer plutôt que déplacer » — visible V2.2 ?
- Brief §16 : option case à cocher globale, off par défaut.
- À garder V2.2 ou simplifier V2.2 = toujours déplacer, dupliquer en V2.2.1 ?
- **Reco Claude** : garder dès V2.2 — seulement 5 lignes de code de plus.
- **Réponse** : *(en attente)*

### D2. Cycle automatique « rêverie → completed »
- Brief §16 : quand carnet `locked` ET rêverie n'a plus d'items → `completed` automatique.
- Ce hook s'active dans le patch V2.2 ou plus tard ?
- **Reco Claude** : V2.2 — c'est le cœur du flux.
- **Réponse** : *(en attente)*

---

## E · Sécurité / RGPD

### E1. Backup BDD
- Toujours pas de backup automatique sur Railway.
- V2 ajoute des données précieuses (conversations, archive Hinge — irremplaçable).
- → Backup automatique enfin obligatoire ?
- **Reco Claude** : oui, dès le début V2. Cron Railway → dump → email.
- **Réponse** : *(en attente)*

---

## Synthèse — bloquant pour démarrer V2.0

**Vraiment bloquant** :
- A1, A2 (arbitrage roadmap)
- B3 (qui fait l'extraction Hinge)
- E1 (backup avant données précieuses)

**Non bloquant — défauts proposés acceptables** :
- B1 défaut → couleur par ordre d'arrivée
- B2 défaut → topbar 2 onglets « Carnets · Histoire »
- B4 défaut → édition messages dès V2.0
- C1 défaut → adresse texte seulement V2.1
- C2 défaut → champ texte libre devise
- C3 défaut → bordure pointillée + fond `#F5F0E6`
- D1 défaut → option dupliquer dès V2.2
- D2 défaut → hook V2.2

→ Si Arthur dit « OK pars sur les défauts » + tranche A1/A2/B3/E1, démarrage immédiat de V2.0 (après V1 finie selon Scénario A).
