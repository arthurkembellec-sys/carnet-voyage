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

## Pourquoi pas un « mot de passe oublié » par email

Carnet de voyage n'a pas d'envoi d'emails configuré en production (aucune
variable `SMTP_*` sur Railway). Tant que ce n'est pas branché, le lien passe
par vous — c'est le chemin le plus sûr et le plus simple pour des personnes
de tous âges : elles n'ont ni compte mail à retrouver, ni mot de passe
provisoire à taper sans faute.

Le jour où l'envoi d'emails sera en place, la page « Mot de passe oublié »
pourra envoyer le lien elle-même. Le reste du mécanisme ne changera pas.
