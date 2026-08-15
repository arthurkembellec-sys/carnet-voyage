# AUDIT UX MOBILE — « Notre Histoire » (PWA iPhone)
**Auditeur : Lead Product Designer mobile · 2026-08-15 · base de code : /tmp/cv (v5.16)**
**Référence produit : Polarsteps / Journi. Règles internes : `00_REGLES_ABSOLUES.md` (D3 étendue : mobile d'abord, cibles 44 px, résultat immédiat, toutes orientations).**

---

## 1. DIAGNOSTIC STRUCTUREL — pourquoi Arthur se sent « pas aidé »

### 1.1 Le vrai problème : deux écrans qui font la même chose sans le dire

L'app a deux écrans de préparation qui **se recouvrent sur 4 familles d'actions**, avec des interfaces différentes pour le même objet :

| Action | Rêverie (`carnet_souhait.html`) | Album (`album.html`) | Recouvrement |
|---|---|---|---|
| Carte + épingles | carte complète : poser, glisser, typer, retirer (l. 20-59, 1090-1244) | carte : typer (`enregistrerEpingle`, l. 2133), retirer (l. 2091) | **Oui** — mêmes popups, capacités différentes |
| Planning | « planning-perles » : blocs, modes 🚗🚶🚲, heures, haltes, « pas fait » (l. 61-92, 603-1019) | `vp-days` : cartes de jours plates, drag entre jours, ✕, ＋ étape (l. 100-133, 1826-1980) | **Oui** — 2 UI radicalement différentes pour LA MÊME donnée (`carnet_planning_save`, app.py l. 1697) |
| Ajouter une étape/lieu | recherche + tap carte + « à caser » | `vpOpenAdd` : recherche dans la carte du jour (l. 1930) | **Oui** |
| Retirer une étape | popup carte, pool, blocs | ✕ du planning, ✕ des chips `day-etape`, popup carte | **Oui** — 3 chemins dans l'album seul |

L'utilisateur n'a **aucun moyen de savoir quel écran est « le bon » pour une action donnée**. Il apprend une interface (les perles, les blocs, les modes) en rêverie, puis la retrouve remplacée par une autre (cartes de jours plates) dans l'album — pour le même planning. Le sentiment « les belles présentations ne sont pas exploitées » vient de là : chaque écran réinvente sa présentation au lieu de prolonger celle déjà apprise (violation de la règle d'or §D du fichier de règles : « un visuel validé devient la référence universelle »).

### 1.2 Preuve technique que le recouvrement est destructeur (pas seulement déroutant)

Le planning de l'album **écrase silencieusement le travail fait en rêverie** :
- `album.html` l. 1894-1896 : `save()` sérialise les jours en **listes plates d'ids** (`[[item_id,...],...]`).
- `app.py` l. 1437-1438 (`_trajet_save`) : une liste plate est convertie en **UN bloc unique `{mode:'car', heure:'', fait:1}`**.
- Conséquence : un simple glisser-déposer d'étape dans le planning de l'album **détruit** les modes (à pied/vélo), les heures, le découpage trajets/haltes et les marquages « pas fait » posés en rêverie. C'est un « fallback muet » interdit par la règle R4, et la cause matérielle du sentiment de perte de maîtrise.

### 1.3 Aucune passerelle visible entre les deux écrans

- La rêverie liste ses voyages issus (l. 94-111). L'album, lui, **n'a aucun lien retour vers sa rêverie d'origine** : topbar = « ← Notre Histoire » + « Fiche » (l. 3-8). L'utilisateur qui veut retoucher son planning « riche » ne peut pas y retourner.
- Le bouton « ✨ Passer en voyage » (rêverie l. 70-74) et le lien texte « Transformer en voyage — mode détaillé » (l. 131-135) sont **deux entrées concurrentes vers la même transformation**, à deux endroits de la page, sans hiérarchie.

### 1.4 Délimitation proposée (une phrase de mission par écran)

- **Rêverie** : *« On imagine : épingler des idées, tracer les trajets, construire le planning. »* — C'est ici, et seulement ici, que le planning **se construit** (blocs, modes, heures, haltes).
- **Album** : *« On raconte le voyage vécu : les photos arrivent, on ajuste ce qui a vraiment eu lieu. »* — Le planning y est en **lecture + ajustement du réel** : retirer (« pas fait »), ajouter (« découvert sur place »), déplacer une étape de jour. Pas de reconstruction.
- **Aperçu** : *« On met en page le livre : couverture, format, marges — puis PDF. »*

### 1.5 Ce qui doit bouger

1. **P0 — Arrêter la perte de données** : le `save()` du planning album (l. 1893-1912) doit reconstruire le format blocs (conserver `mode/heure/fait` des blocs existants, ne réécrire que l'appartenance aux jours), ou la route doit fusionner au lieu d'écraser.
2. **P1 — Unifier la présentation du planning** : reprendre la rangée de perles (composant déjà validé) comme tête du planning album ; les cartes `day-card` deviennent le détail du jour sélectionné. Un seul langage visuel appris une fois.
3. **P1 — Lien croisé permanent** : dans l'album d'un voyage issu d'une rêverie, une ligne sous le planning : « Planning préparé dans la rêverie *{titre}* → la rouvrir ». (La donnée existe : `payload.from_souhait_id`, app.py l. 1892.)
4. **P2 — Une seule entrée de transformation** : garder « ✨ Passer en voyage » ; le « mode détaillé » devient une option *dans* l'écran de confirmation, pas un second lien perdu en bas de page.

---

## 2. BUGS D'ACCESSIBILITÉ MOBILE — les croix du planning en portrait

### 2.1 Cause n° 1 (rêverie) : la rangée d'outils du bloc déborde et `overflow: hidden` coupe les croix

**Fichiers** : `static/style.css` l. 4034-4103 ; HTML généré par `blocHtml()` (`carnet_souhait.html` l. 805-833).

Arithmétique en portrait 390 px :
- Largeur disponible dans `.day-bloc-head` : 390 − 40 (`.page` padding 20×2, css l. 208) − 36 (`.planning-card` 18×2, l. 3490) − 28+4 (`.pearl-detail` 14×2 + border-left 4, l. 3976-3978) − 2 (bordures `.day-bloc`) − 16 (padding head 8×2, l. 4043) ≈ **264 px**.
- Largeur minimale des enfants (aucun ne peut céder) : `.bloc-heure` min-width 76+2 (l. 4056) + `.bloc-modes` flex:none ≈ 81 px (3 boutons, l. 4058-4063, mobile l. 4100) + `.bloc-duree` min-width 52 + `white-space: nowrap` (l. 4074-4079, 4102) + `.bloc-fait` ≈ 29 px (l. 4325) + `.bloc-out` ≈ 28 px (l. 4080) + 4 gaps de 6 px = **≈ 292 px**.
- **292 > 264** : la rangée déborde de ~28 px — exactement la largeur du ✕ — et `.day-bloc { overflow: hidden }` (l. 4039) **le coupe physiquement hors de l'écran**. En paysage (~844 px), tout tient : le bug n'existe qu'en portrait. C'est mot pour mot le constat d'Arthur.

**Correctif CSS exact** (dans le bloc `@media (max-width: 600px)` existant, l. 4099) :

```css
@media (max-width: 600px) {
  .day-bloc-head { flex-wrap: wrap; row-gap: 2px; }
  .bloc-heure { flex: 0 1 84px; min-width: 72px; }
  /* la durée descend sur sa propre ligne au lieu de pousser les croix dehors */
  .bloc-duree { order: 6; flex: 1 1 100%; min-width: 0; text-align: left;
                padding-left: 2px; }
  /* cibles tactiles dignes (règle D3 : 44 px) */
  .bloc-fait, .bloc-out { min-width: 44px; min-height: 44px; font-size: 15px; }
}
```
Aucun JS à changer ; `overflow: hidden` peut rester (il ne sert plus qu'aux coins arrondis).

### 2.2 Cause n° 2 (album, `vp-days`) : croix de 26 px, positionnée en absolu, sous un drag instantané

**Fichiers** : `static/style.css` l. 5237-5251 ; `album.html` l. 1855-1890.

Trois violations directes de D3 :
1. `.day-step-del` fait **26×26 px, font-size 12 px** (l. 5246) — sous le minimum de 44 px ; sur un `day-step` de pleine largeur en portrait, le pouce rate la croix ou touche l'étape.
2. Elle est en **`position: absolute; right: 2px`** (l. 5240-5242) — D3 interdit explicitement le positionnement absolu pour un outil (« les actions vivent dans le flux (flex) »). Le `li` est rempli par `li.textContent = …` (`album.html` l. 1862) : le titre n'est **pas** dans un `<span.day-step-title>`, donc les règles d'ellipse (css l. 4001) ne s'appliquent pas — un titre long recouvre la zone de la croix.
3. Le Sortable des jours est créé **sans `delayOnTouchOnly`** (`album.html` l. 1888 : `new Sortable(ul, { group:'vp', animation:150, onEnd:save })`) : au doigt, le moindre micro-mouvement du tap déclenche un drag au lieu du clic sur ✕. La grille d'idées, elle, le fait correctement (`carnet_souhait.html` l. 1461-1462 : `delay: 50, delayOnTouchOnly: true`). Même défaut sur les Sortable du planning rêverie (l. 861, 864, 927).

**Correctif exact** :

CSS (remplacer l. 5238-5251) :
```css
.day-step { padding-right: 8px; }               /* au lieu de 30px réservés à l'absolu */
.day-step-del {
  position: static; transform: none;            /* retour dans le flux flex */
  flex: none; margin-left: auto;
  width: 44px; height: 44px; font-size: 14px;
  border: none; background: transparent; color: var(--ink-whisper);
  border-radius: 50%; cursor: pointer;
}
```
JS (`album.html`, fonction `render()`, l. 1862) — wrapper le titre pour l'ellipse :
```js
var t = document.createElement('span');
t.className = 'day-step-title';
t.textContent = (PIN_EMOJIS[e.pin_kind] ? PIN_EMOJIS[e.pin_kind] + ' ' : '') + e.title;
li.appendChild(t);            // remplace li.textContent = …
```
JS (l. 1888, et `carnet_souhait.html` l. 861, 864, 927) :
```js
new Sortable(ul, { group: 'vp', animation: 150, delay: 150, delayOnTouchOnly: true, onEnd: save });
```

### 2.3 À vérifier dans la même passe
- `.pin-remove` des popups carte : les popups Leaflet en bas d'écran passent sous le footer FAB (`.album-footer`) sur petits écrans — tester à 390×664 avec clavier fermé.
- `.item-delete` est bien montée à 40 px sur mobile (css l. 3811) : porter à 44 px pour être conforme à la lettre de D3.

---

## 3. RENOMMAGE DES PINS

### 3.1 Constat : l'API n'existe pas
`app.py` expose pour un item : `/item/<id>/geo` (l. 1633, `item_update_geo`), `/item/<id>/pin_kind` (l. 1653, `item_set_pin_kind`), `/item/<id>/parent` (l. 1671), `/item/<id>/supprimer` (l. 1724), `/item/<id>/restaurer` (l. 1739). **Aucune route ne modifie `title`** (ni `address`, ni `body`). Le blocage d'Arthur est d'abord serveur.

### 3.2 Route à créer (sur le modèle exact de `item_set_pin_kind`)
```python
@app.route('/item/<int:item_id>/titre', methods=['POST'])
@couple_required
def item_set_title(item_id):
    if not csrf_check():
        return jsonify({'ok': False, 'error': 'CSRF'}), 403
    item = query("SELECT ci.id, c.couple_id FROM carnet_items ci "
                 "JOIN carnets c ON c.id=ci.carnet_id WHERE ci.id=?",
                 (item_id,), one=True)
    if not item or item['couple_id'] != current_espace_id():
        return jsonify({'ok': False, 'error': '404'}), 404
    title = (request.form.get('title') or '').strip()[:120]
    if not title:
        return jsonify({'ok': False, 'error': 'Titre vide'}), 400
    execute("UPDATE carnet_items SET title=? WHERE id=?", (title, item_id))
    return jsonify({'ok': True, 'title': title})
```

### 3.3 Où l'éditer (par ordre de valeur)

1. **Popup carte — l'endroit où Arthur a essayé.** `pinPopupHtml()` (`carnet_souhait.html` l. 1090-1115) affiche `<strong>{titre}</strong>` figé alors que le popup a déjà un sélecteur de type + bouton OK. Spec : remplacer le `<strong>` par `<input class="pin-title-edit" value="{titre}" maxlength="120">` ; le bouton **OK existant** (l. 1107) enregistre titre **et** type en un seul geste (2 fetchs, ou une route commune `/item/<id>/modifier`). Après succès : `p.data.title = t; m.setTooltipContent(label)` + `setSaved('Épingle renommée')` — résultat immédiat, règle D3. Le popup de création (l. 1254-1265) a déjà ce champ : **la cohérence création/édition est le geste attendu**.
2. **Popup carte de l'album** (`album.html` l. 2087-2093) : même spec, même composant — c'est le même popup partagé, il doit rester identique (règle R6).
3. **Planning** : pas d'édition inline dans les `day-step` (trop dense au doigt) ; un tap sur le titre d'une étape ouvre son popup carte (la carte centre l'épingle) — un seul lieu d'édition, zéro divergence.
4. **Liste des idées** (`_souhait_item.html` l. 23, `.item-title`) : tap sur le titre → input inline + ✓. Confort, en second temps.

---

## 4. SYSTÈME D'AIDE — sobre, contextuel, sans tour guidé

### 4.1 Principe (3 mécanismes, aucun bloquant)

1. **Bandeau « première fois »** par écran : une carte douce sous le hero, affichée tant que l'utilisateur ne l'a pas fermée (`localStorage 'nh_hint_<ecran>'`). Une seule phrase de mission + 3 gestes. Bouton « Compris ». Jamais de re-pop.
2. **Bouton « ? » permanent** dans la topbar de chaque écran : rouvre ce même bandeau (l'aide reste trouvable après fermeture — c'est la lacune classique des tooltips jetables).
3. **Hints en creux existants à renforcer** : l'app a déjà la bonne grammaire (`.reverie-map-hint` l. 36, `.planning-hint` l. 76, `.route-days-hint`) — les généraliser aux zones muettes : le `vp-days` de l'album et l'aperçu n'ont aucun hint.

Coût : ~40 lignes de JS partagé (`_base.html`) + un partial `_hint_card.html`.

### 4.2 Microcopies exactes (français)

**Rêverie — bandeau première fois :**
> **Ici, on imagine le voyage.**
> 📍 Touche la carte ou cherche un lieu pour épingler une idée.
> 🧭 « Tracer le parcours » relie tes épingles ; attribue chaque trajet à un jour.
> 📅 Pose les dates : le fil des journées apparaît, tout s'enregistre tout seul.
> Quand c'est prêt : **✨ Passer en voyage** — la rêverie reste intacte ici.

**Album — bandeau première fois :**
> **Ici, on raconte le voyage vécu.**
> 📷 Ajoute les photos : elles se rangent seules par journée (date et lieu).
> 🧭 Le planning montre le prévu — retire ce qu'on n'a pas fait, ajoute ce qu'on a découvert : rien n'est perdu, tout part en corbeille restaurable.
> ¶ Marge : billets, anecdotes, gribouillis — ils apparaîtront dans la marge du livre.
> Quand l'album te plaît : **📖 Le livre** en haut à droite.

**Aperçu livre — bandeau première fois :**
> **Ici, on met en page le livre.**
> Touche la couverture pour la changer ; choisis format et position de la marge.
> Ce que tu vois est ce qui s'imprime — le PDF se génère en bas de page.
> Un doute sur une photo ? Retourne à l'album, le livre suivra.

**Hints permanents à ajouter :**
- Album, sous `vp-days` (`album.html` l. 111) : *« Le prévu vient de ta rêverie. Glisse une étape vers un autre jour si la réalité a changé — ✕ la retire (corbeille), ＋ en ajoute une. »*
- Album, viewer (voir §5) : *« Reste appuyé sur la photo pour y épingler une note. »*
- Rêverie, barre parcours (l. 41-45), compléter : *« Touche les épingles dans l'ordre du voyage — retouche une épingle pour un aller-retour. »*

---

## 5. COMMENTAIRES PHOTO (épingles de notes, v5.8)

### 5.1 Pourquoi ce n'est pas intuitif (`album.html` l. 476-486, 1700-1770 ; css l. 4846-4891)

1. **C'est un « mode » invisible.** « ✎ Épingler une note » (l. 477) n'épingle rien : il **arme un état** où le prochain tap sur l'image pose l'épingle. Le libellé promet une action, livre un mode — l'utilisateur tape le bouton, il ne se passe rien de visible sur la photo, il retape, ça désarme. Le hint (l. 480) apparaît à côté du bouton, pas là où il faut agir.
2. **Le tap est ambigu.** Le viewer écoute aussi le swipe (l. 1815-1824, seuil 60 px) : un tap un peu glissé en mode épingle **change de photo** et le mode reste armé sur la suivante — comportement erratique vécu comme un bug.
3. **Rien n'annonce la fonctionnalité en lecture.** Épingles de 22 px (css l. 4848) et pastille de compte discrète : la découvrabilité repose entièrement sur un bouton au libellé abstrait.
4. **Le popover de saisie** (l. 1741-1770) se positionne près du point tapé ; sur iPhone le clavier recouvre la moitié basse : saisir une note sur le bas d'une photo se fait à l'aveugle.

### 5.2 Le geste standard du métier

Le standard (Google Photos/commentaires, Figma, apps d'annotation) : **l'appui long sur l'image pose l'épingle à l'endroit pressé**, et la saisie s'ouvre en **feuille basse** au-dessus du clavier. Spec :

1. **Appui long (450 ms) sur `#viewer-img`** → vibration légère (`navigator.vibrate(10)`), épingle fantôme posée, feuille basse de saisie (`.pin-sheet`, même patron que les modales feuille, css l. 3829-3836). Le swipe reste réservé au glissement > 60 px : aucun conflit.
2. **Garder le bouton ✎** comme chemin découvrable, mais qu'il **agisse** : tap → l'épingle se pose au centre de la photo, **glissable** vers sa cible, feuille de saisie ouverte. Plus de mode armé, plus de hint nécessaire.
3. **Lecture** : épingles à 28 px minimum, apparition animée à l'ouverture du viewer (200 ms, scale 0→1) pour signaler leur existence ; la pastille `.tile-notes-count` passe à `𝑛 ¶` pour dire « notes », pas juste un chiffre.
4. Supprimer l'état `pinMode` (l. 1661, 1700-1715) — moins de code, moins d'états.

---

## 6. PRIORISATION

### Quick wins (< 1 h chacun)

| # | Impact | Fix | Où |
|---|---|---|---|
| 1 | ★★★ | Croix du bloc planning coupées en portrait : `flex-wrap` + `.bloc-duree` sur sa ligne + cibles 44 px | css l. 4099-4103 (§2.1) |
| 2 | ★★★ | `.day-step-del` : 44 px, retour dans le flux flex, titre dans `.day-step-title` | css l. 5238-5251, album.html l. 1862 (§2.2) |
| 3 | ★★★ | `delay:150, delayOnTouchOnly:true` sur les 4 Sortable du planning | album.html l. 1888 ; carnet_souhait.html l. 861, 864, 927 |
| 4 | ★★★ | Route `POST /item/<id>/titre` + input titre dans le popup partagé (rêverie + album) | app.py (§3.2), carnet_souhait.html l. 1091, album.html l. 2087 |
| 5 | ★★ | Hint permanent sous le planning album + hint « appui long » dans le viewer | album.html l. 111, 480 |
| 6 | ★★ | Lien retour album → rêverie d'origine | album.html topbar (§1.5.3) |
| 7 | ★ | `.item-delete` 40→44 px | css l. 3811 |

### Chantiers (½ j à 2 j)

| # | Impact | Chantier |
|---|---|---|
| A | ★★★ | **Stopper l'écrasement du planning riche par l'album** : `save()` de vp-days reconstruit le format blocs (mode/heure/fait préservés). C'est de la perte de données utilisateur — avant tout le reste des chantiers. (§1.2, ½ j) |
| B | ★★★ | **Épingles de notes photo au geste standard** : appui long + feuille basse, suppression du mode armé. (§5.2, 1 j) |
| C | ★★ | **Système d'aide** : partial `_hint_card.html` + « ? » topbar + microcopies §4.2. (½ j) |
| D | ★★ | **Unification visuelle du planning album sur les perles** + clarification des missions d'écran (§1.4-1.5). (1-2 j) |
| E | ★ | Fusion des deux entrées de transformation en une. (§1.5.4) |

**Ordre recommandé : 1-2-3 (le planning redevient utilisable au doigt) → A (plus de perte silencieuse) → 4 (les pins se renomment) → C+5 (l'utilisateur se sent guidé) → B → D → E.**
