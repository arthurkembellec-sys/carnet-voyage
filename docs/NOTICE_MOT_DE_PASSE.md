# Notice — Mot de passe oublié

> Notre Histoire · v5.4 (2026-07-28)

---

## Pour la personne qui n'arrive plus à se connecter

**Trois gestes. Rien à retenir, rien à recopier.**

### 1. Demandez votre lien

Sur la page de connexion, touchez **« Mot de passe oublié ? »**.
L'écran vous dit à qui écrire. Un message, un appel — dites simplement :
« je n'arrive plus à me connecter ».

### 2. Ouvrez le lien reçu

Vous recevez un lien par SMS ou par message. **Touchez-le** : il ouvre
directement la page où choisir votre mot de passe.

### 3. Choisissez votre mot de passe

Huit caractères au minimum. Écrivez-le deux fois pour être sûr, validez.
Reconnectez-vous avec.

**Bon à savoir**

- Le lien ne fonctionne **qu'une seule fois** et cesse d'être valable au bout
  de **24 heures**. S'il ne marche plus, redemandez-en un : c'est sans souci.
- Vos carnets, vos photos et vos souvenirs ne bougent pas. Seul le mot de
  passe change.
- Si quelqu'un était connecté à votre compte sur un autre téléphone, il en
  est sorti au moment où vous avez choisi votre nouveau mot de passe.

---

## Pour l'administrateur

L'espace admin de Carnet de voyage est à **`/admin`** (menu de la barre du
haut → *Administration*). AqGK a le sien, de son côté : deux applications,
deux bases, deux espaces — **aucune ne peut lire les comptes de l'autre**.

### Réinitialiser le mot de passe de quelqu'un

1. `/admin` → **Les comptes**.
2. Cherchez la personne (nom ou email).
3. **Créer un lien**.
4. Le lien s'affiche. **Copier le lien**, ou **Envoyer par SMS** (ouvre
   l'application de messages avec le texte déjà écrit).
5. Envoyez-le à la personne. Elle fait le reste.

### Ce qu'il faut savoir avant de le faire

- **Le lien ne s'affiche qu'une fois.** Ni la base ni les journaux ne le
  gardent : on n'en garde que l'empreinte. Si vous le perdez, refaites-en un
  — le précédent cesse aussitôt de fonctionner.
- Un lien vaut **24 heures** et **un seul usage**.
- Créer un nouveau lien pour quelqu'un **annule** celui d'avant.
- Vous ne voyez jamais le mot de passe de personne, ni avant ni après : c'est
  la personne qui le choisit.
- Transmettez le lien par un canal que vous maîtrisez (SMS, WhatsApp, de vive
  voix). Quiconque a le lien peut choisir le mot de passe pendant 24 heures.

### Qui est administrateur

Les emails listés dans la variable d'environnement `ADMIN_EMAILS`
(par défaut : `arthur.kembellec@gmail.com`). Un compte ordinaire qui tente
d'ouvrir `/admin` reçoit un 403.

---

## Brancher l'envoi d'emails (Resend) — la manip

Le code est **déjà en place**. Tant que la variable `RESEND_API_KEY` est
absente, l'app ne fait pas semblant : la page « Mot de passe oublié » garde le
chemin par l'administrateur. Dès que la clé est posée, la même page affiche un
champ email et envoie le lien toute seule. **Aucun déploiement à refaire** —
Railway redémarre le service quand une variable change.

### Bonne nouvelle : la moitié du travail est déjà faite

AqGK utilise déjà Resend avec le domaine `aqgk.fr` vérifié. Carnet est sur
`histoire.aqgk.fr`, un sous-domaine du même domaine : **la même clé fonctionne**,
et on peut envoyer depuis n'importe quelle adresse `@aqgk.fr`.

### Le chemin le plus court (2 minutes)

Réutiliser la clé d'AqGK. Depuis le tableau de bord Railway :

1. Projet **AqGK** → service → onglet **Variables** → copier la valeur de
   `RESEND_API_KEY`.
2. Projet **confident-gratitude** (Carnet) → service **web** → **Variables**
   → **New Variable** :
   - `RESEND_API_KEY` = la valeur copiée
   - `MAIL_FROM` = `Notre Histoire <histoire@aqgk.fr>`
3. Railway redéploie tout seul (~1 min). Vérifier sur
   `https://histoire.aqgk.fr/mot-de-passe-oublie` : un champ email doit être apparu.

En ligne de commande, depuis le dossier du projet, même résultat :

```bash
railway variables --service web --set "MAIL_FROM=Notre Histoire <histoire@aqgk.fr>"
```

Puis la clé, à coller à la place de `LA_CLE` (elle ne doit apparaître ni dans un
fichier du dépôt, ni dans un historique partagé) :

```bash
railway variables --service web --set "RESEND_API_KEY=LA_CLE"
```

### Si vous préférez une clé dédiée à Carnet

Sur [resend.com](https://resend.com) → **API Keys** → **Create API Key**,
permission *Sending access*, domaine `aqgk.fr`. Puis les deux mêmes variables.
Une clé séparée se révoque sans toucher à AqGK — c'est plus propre si un jour
les deux apps se séparent.

### Une fois branché

- La page « Mot de passe oublié » affiche un champ email et envoie le lien.
- La réponse est **la même** que l'adresse existe ou non : la page ne dit jamais
  qui a un compte.
- **3 demandes par heure et par compte** au maximum, pour qu'on ne puisse pas
  s'en servir pour inonder quelqu'un de messages.
- Le chemin par l'administrateur (créer un lien à la main) **continue de
  marcher** : c'est le secours pour qui n'a pas accès à sa boîte mail.
