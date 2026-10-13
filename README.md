# AI Hub

**Plusieurs IA, un seul espace de travail, des accès que vous choisissez.**

AI Hub réunit Claude Code, ChatGPT via Codex et vos modèles locaux Ollama dans
une interface en français. Discutez avec une IA, comparez leurs réponses ou
faites travailler plusieurs agents ensemble. Retrouvez vos conversations et
vos projets depuis votre ordinateur ou votre téléphone.

**Version 1.0.4 · Windows et Linux · Licence GPL-3.0**

[Télécharger la dernière version](https://github.com/Demonwal51/ai-hub/releases/latest)
· [Voir les changements](CHANGELOG.md) · [Licence](LICENSE)

## Ce que vous pouvez faire

- **Choisir les accès** : fichiers sélectionnés, réseau et mémoire. Les
  limites d’un projet s’appliquent aussi à ses conversations.
- **Travailler sur des copies** : lire ou modifier votre sélection, voir les
  différences et décider quels résultats appliquer aux originaux.
- **Connecter vos propres comptes IA** : une connexion séparée par personne,
  espace et fournisseur, sans recopier les identifiants du PC dans la VM.
- **Faire travailler plusieurs IA** : un chef propose un plan, vous le
  validez, les agents réalisent les sous-tâches puis une synthèse est produite.
- **Garder le fil** : recherche, résumés, consignes, favoris, épingles,
  archives et modèles de travail réutilisables.
- **Partager votre serveur** : rôles, invitations et conversations privées
  ou partagées. Les membres et invités doivent utiliser une exécution isolée.
- **Travailler à votre rythme** : tâches autonomes avec limites, demandes
  planifiées, suivi des quotas, pièces jointes et comparaisons entre IA.
- **Utiliser votre téléphone** : interface adaptée, accès privé par Tailscale,
  notifications et lecture hors connexion si vous l’activez sur l’appareil.

## Un premier espace en trois étapes

Le bouton **Bien démarrer** vous guide pour créer un espace, connecter votre
IA et vérifier le fonctionnement. Trois profils préparent les accès ; vous
pouvez ensuite les personnaliser.

| Profil | Accès aux fichiers du PC | Usage |
| --- | --- | --- |
| Discussion seule | Aucun fichier | Poser une question ou développer une idée |
| Analyse de fichiers | Lecture d’une copie de la sélection | Comprendre du code ou analyser un document |
| Travail sur un projet | Modification d’une copie de la sélection | Préparer des changements à vérifier |

Les espaces isolés nécessitent **Docker Sandboxes** sur le serveur. Votre
compte Claude ou ChatGPT se connecte dans **Comptes IA de cet espace**. Une
connexion effectuée dans un autre espace n’est pas automatiquement réutilisée.

**Accès de cette IA** présente les accès effectifs et la portée du compte.
Le propriétaire peut utiliser **Tout couper** : les nouveaux travaux et
connexions sont bloqués durablement, et l’arrêt des travaux en cours est
demandé. Les données et comptes sont conservés. Seule une réactivation
explicite rétablit l’exécution ; changer les permissions ne lève pas la pause.
Si un arrêt n’est pas confirmé, l’interface le signale et propose de réessayer.

## Vérifier les résultats avant de les appliquer

**Vérifier les fichiers produits** affiche les fichiers créés, modifiés ou
supprimés. Consultez les différences, récupérez la copie en ZIP ou sélectionnez
les fichiers à appliquer. L’application vérifie les conflits avec les
originaux et conserve une sauvegarde avant modification. Les suppressions
exigent une confirmation explicite.

## Créer ses fichiers et dossiers

Dans les formulaires de nouvelle conversation et de projet, **Créer ce dossier
sur le serveur** prépare le dossier de travail qui manque. Dans le menu d’une
conversation, **Créer un fichier ou un dossier…** ajoute un fichier vide ou un
sous-dossier au dossier de travail, avec les parents manquants si vous le choisissez.

Ces actions sont réservées au propriétaire et aux administrateurs. Elles
respectent les dossiers autorisés, demandent votre mot de passe si votre compte
en possède un et refusent d’écraser un fichier existant. La création se fait
sur le PC serveur, même depuis un téléphone ou un deuxième PC. Les permissions
de l’IA restent celles choisies pour l’espace.

## Installation Windows

1. Téléchargez `ai-hub-1.0.4.zip` dans les
   [versions publiées](https://github.com/Demonwal51/ai-hub/releases/latest), puis extrayez-le.
2. Installez Python **3.12 ou plus** si nécessaire.
3. Lancez `installer.bat`. Il prépare l’environnement Python, installe les
   dépendances et crée un raccourci sur le Bureau.
4. Ouvrez **AI Hub** ou `Lancer AI Hub.bat`, puis créez votre serveur.

Le serveur démarre par défaut sur `http://127.0.0.1:8770/`. Le dossier `data/`
contient les données de cette installation. Conservez-le lors d’une mise à jour.

## Installation Linux

L’installation et le démarrage de l’application ont été vérifiés sur
**Ubuntu 26.04, Debian 13 et Fedora 44**.

1. Installez Python **3.12 ou plus**, ainsi que le support `venv` et `pip` de
   votre distribution.
2. Téléchargez et extrayez `ai-hub-1.0.4-linux.tar.gz`.
3. Depuis le dossier extrait, lancez :

```sh
sh installer.sh
./lancer.sh
```

L’installateur ne lance pas `sudo`. Il prépare l’environnement local et un
lanceur de bureau. `AIHUB_NO_SHORTCUT=1 sh installer.sh` installe sans raccourci.

Le moteur Docker Sandboxes natif Linux nécessite un hôte pris en charge et
KVM. Il n’a pas été validé sur un hôte Linux natif lors de ces essais. La
compatibilité de l’application Debian/Fedora ne signifie pas que Docker
prend en charge son moteur local sur ces distributions.

## Fournisseurs et comptes

- **Claude Code** : CLI officielle, avec votre compte Claude. Sur Windows,
  AI Hub peut détecter la copie installée par l’application Claude.
- **Codex** : CLI officielle et protocole `app-server`, avec votre compte
  ChatGPT. Les modèles proposés dépendent de la CLI installée et de votre accès.
- **Ollama** : utilise votre installation locale existante. AI Hub n’en
  installe pas une autre. Dans un espace isolé, l’inférence reste sur le
  service local en boucle locale et les outils de fichiers sont en lecture seule.

Les limites et la facturation des fournisseurs restent celles de vos comptes.
Les CLI officielles gèrent leurs identifiants ; AI Hub utilise leurs commandes
de connexion et d’état sans lire leurs fichiers d’identifiants.

## Organisation et collaboration

Les projets regroupent les conversations, un dossier de travail et des
consignes. Vous choisissez si leur mémoire peut être partagée : les contrôles
d’accès continuent de s’appliquer à chaque personne et conversation.

Les épingles et archives appartiennent à chaque personne. Les filtres par
projet, IA et date s’appliquent aussi à la recherche. Les modèles de travail
préparent un brouillon à relire avant envoi.

En **Orchestration**, le chef propose les sous-tâches, leurs dépendances et les
agents. Vous pouvez modifier le plan avant **Valider et lancer**. Les tâches
indépendantes avancent en parallèle ; celles qui partagent des fichiers sont
coordonnées. Vous pouvez aussi discuter avec le chef ou lui demander d’adapter
le plan.

## Données, sécurité et sauvegardes

Les conversations et réglages sont stockés sur votre serveur. Un fournisseur
IA distant reçoit la demande et le contexte que vous lui transmettez ; utiliser
un compte cloud ne rend donc pas ses traitements locaux.

- Espaces isolés exécutés dans des microVMs, accès sélectionnés et aucun repli
  automatique sur le PC lorsque le moteur est indisponible.
- Comptes et espaces séparés ; les identifiants globaux partagés du moteur
  sont refusés. Recréer un compte ou une conversation ne récupère pas
  l’ancienne VM connectée par réutilisation d’un numéro.
- Révocation des accès, arrêt des travaux des comptes désactivés et contrôles
  de l’identité pour les copies conservées hors connexion.
- Sauvegardes quotidiennes de la base avec les pièces jointes référencées.
  Une restauration est préparée et vérifiée avant remplacement.
- Mise à jour avec sauvegarde, contrôle du nouveau serveur et retour du code
  et de la base si le démarrage échoue.

La version **1.0.1 migre la base au schéma 24**. Sauvegardez-la avant mise à
jour : une ancienne version du code ne peut pas ouvrir un schéma plus récent.
Le retour de mise à jour ne restaure pas les dépendances Python modifiées.
Les anciens backups qui ne contenaient que la base ne peuvent pas recréer
les octets de pièces jointes manquantes.

## Téléphone et diagnostic

Pour joindre le serveur depuis un téléphone, utilisez un accès privé Tailscale
avec l’authentification d’AI Hub. Le PC doit rester disponible pour de nouvelles
réponses. La lecture hors connexion concerne seulement les copies que vous
avez choisi de conserver sur cet appareil.

Le menu **Diagnostic** vérifie la base, le disque, le moteur d’isolation et le
compte de l’espace choisi, sans envoyer de demande à une IA. Une action précise
est proposée lorsqu’un composant manque. Les thèmes clair, sombre et système
ainsi que la densité d’affichage se règlent par appareil.

## Vérifications de la 1.0.1

- Suites de **684 tests** validées sous Windows et Ubuntu, avec respectivement
  une et trois exclusions liées à l’environnement.
- **63 tests ciblés** réussis sur Debian et Fedora, installation et démarrage
  du paquet vérifiés avec un utilisateur sans droits administrateur.
- Paquets démarrés et sonde de santé vérifiée sur les quatre systèmes. Les
  dépendances déjà installées ont été réutilisées pour ces essais.
- Connexions et réponses réelles Claude/Codex validées pendant la préparation
  de la 1.0 ; les nouvelles protections d’annulation de la 1.0.1 sont
  testées avec des CLI simulées.
- Interface testée sur ordinateur et en largeur téléphone de 393 pixels.

Ces vérifications ne remplacent pas les essais d’un moteur Linux natif, d’un
téléphone réel ou des services externes soumis à leurs propres permissions.

## Développement

Créez un environnement avec `python -m venv .venv`.

Sous Linux :

```sh
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -B -m unittest discover -t . -s tests
```

Sous Windows :

```bat
.venv\Scripts\python -m pip install -r requirements-dev.txt
.venv\Scripts\python -B -m unittest discover -t . -s tests
```

Les tests utilisent de fausses CLI et ne consomment pas vos quotas IA.
`tools/build_release.py` fabrique les paquets depuis le dernier commit Git,
en excluant les données locales, environnements, tests et outils de développement.

AI Hub est un logiciel libre sous [GPL-3.0](LICENSE).
