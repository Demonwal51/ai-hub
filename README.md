# AI Hub

**Plusieurs IA, un seul espace de travail, des accès que vous choisissez.**

AI Hub réunit Claude Code, ChatGPT via Codex et vos modèles locaux Ollama dans
une interface en français. Discutez avec une IA, comparez leurs réponses ou
faites travailler plusieurs agents ensemble. Retrouvez vos conversations et
vos projets depuis votre ordinateur ou votre téléphone.

**Version 1.3.0 · Windows et Linux · Licence GPL-3.0**

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

## Commencer

L’accueil est la demande : écrivez ce que vous voulez, choisissez l’IA (et un
projet si vous voulez), puis **Envoyer**. La conversation se crée toute seule et
prend le début de la demande pour titre. Sans dossier choisi, elle reçoit un
dossier vide rien qu’à elle sur le PC serveur. **Plus d’options…** ouvre les
autres choix : modèle de travail, dossier, plusieurs IA, espace isolé,
permissions.

L’écran ressemble à Claude Code : la liste à gauche, la conversation au
centre, une seule zone de saisie en bas avec 📎, l’IA, ce qu’elle peut faire
sans demander, et la flèche ↑. Le menu **⋯** d’une conversation la renomme,
l’archive, la range dans un projet, lui donne des consignes, la relie à
Obsidian (un nouveau coffre ou un coffre existant), l’exporte ou la supprime. **Réglages** (en bas à gauche) donne le quota, l’apparence, les
notifications, les mises à jour et les IA proposées.

Le reste est derrière **Réglages → Fonctions avancées** (sur chaque appareil) :
plusieurs IA avec un chef, travail autonome, comparer deux IA, favoris,
planifier, espaces isolés, outils, mémoire, partage, coffres Obsidian,
personnes, sauvegardes, import.

## Mises à jour

**⬆ Mise à jour à faire** apparaît en bas à gauche quand il y en a une, et
**Réglages → Mises à jour** dit où en est l’appli :

- une version plus récente publiée sur GitHub : depuis une installation `.zip`,
  **Installer** la télécharge, vérifie son empreinte (celle publiée dans
  `SHA256SUMS`), l’installe et redémarre (l’ancienne version et la base sont
  gardées dans `data\mise-a-jour`). Si la nouvelle version ne répond pas,
  l’ancienne revient et `data\mise-a-jour\journal.txt` dit pourquoi ;
- le code changé sur ce PC depuis le démarrage du serveur (une copie git, après
  un `git pull` ou un commit) : **Redémarrer sur le nouveau code** l’essaie
  d’abord à part (il se charge et ouvre une copie de la base) et ne redémarre que
  si l’essai passe, qu’aucune IA ne travaille et qu’aucun fichier du code n’est
  en train de changer. Si la base change de forme, elle est copiée à côté avant.

Une page déjà ouverte, sur un téléphone par exemple, propose **Recharger la
page** quand le serveur tourne sur une autre version.

## Un espace isolé

Trois profils préparent les accès d’un espace isolé (**Plus d’options…** ou
**⋯ → Espace et accès**) ; vous pouvez ensuite les personnaliser.

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

## Reprendre un travail interrompu

La dernière réponse interrompue propose **Reprendre**. Un plan déjà validé et
interrompu propose **Reprendre le plan** : seules ses tâches inachevées repartent,
les résultats acquis restent conservés. Si seule la synthèse a été interrompue,
elle peut être reprise sans rejouer les tâches.

Les points de reprise et sessions sont enregistrés pendant le travail. La
reprise est manuelle et utilise les accès et comptes IA actuels. Une session
appartient à son compte et à son contexte ; si elle n’est plus disponible, l’IA
repart avec le dernier texte sauvegardé et doit vérifier l’état des fichiers.
Une commande externe coupée en cours ne peut pas être restaurée à l’instruction
près. Le dossier de travail d’un plan commencé doit rester le même.

## Mémoire RAG des projets

Dans une conversation, ouvrez **⋯ → Mémoire RAG…**. Retrouvez les passages des
conversations auxquelles vous avez accès et ajoutez volontairement des documents
ou notes au projet. Les formats texte Markdown, TXT, CSV, JSON et YAML sont
acceptés ; vous pouvez aussi coller une note. Le propriétaire du projet gère
ses sources, qui deviennent consultables par ses lecteurs.

Les documents sont découpés et indexés dans SQLite. Les passages pertinents,
avec leurs sources, accompagnent les demandes aux IA. La recherche par mots
fonctionne sans Ollama ; la recherche par le sens s’y ajoute lorsque son modèle
d’embeddings est disponible. Aucun modèle n’est téléchargé automatiquement.

Un espace isolé limité à sa propre mémoire ne reçoit pas les documents du
projet. Aucun coffre Obsidian n’est parcouru automatiquement : associez les notes
choisies comme fichiers Markdown. Le retrait d’une source l’exclut des recherches
futures ; il n’efface pas les réponses déjà produites ni le fichier original.

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

## Projets : coffre, notes, quotas, export

- **Coffre des notes de ce projet** (fenêtre du projet, propriétaire) : les notes
  des conversations du projet vont dans ce coffre plutôt que dans celui des
  réglages. Une note qui existe déjà n’est jamais reprise sans le demander.
- **Quota atteint pendant un plan** : le plan propose de reprendre tout seul à
  l’heure du retour du quota, ou de confier ses tâches restantes à une autre IA.
  Rien ne se fait sans votre clic ; les tâches terminées ne sont pas rejouées.
- **Changer d’IA pour la suite…** (sous un plan validé, en cours ou interrompu) : les tâches pas
  encore faites, et le chef si vous le cochez, passent sur l’IA choisie, par exemple Claude seul
  quand ChatGPT n’a plus de quota. Les tâches en cours ou terminées gardent la leur.
- **Exporter le projet (.zip)** puis **Importer → Un projet AI Hub (.zip)** :
  conversations, consignes, accès, documents et notes, sur ce serveur ou un autre.
  Les fichiers du dossier de travail ne sont pas dedans.

## Coffres : chercher, ajouter au projet

Dans le lecteur des coffres, **Chercher dans le texte** trouve les notes qui
contiennent tous les mots. **Ajouter à un projet…** met la note ouverte dans la
mémoire RAG d’un projet ; elle suit son fichier quand il change dans Obsidian.

## Sauvegardes et essais

Menu → **Serveur** liste les sauvegardes : **Restaurer…** remet l’une d’elles,
avec votre mot de passe, après avoir gardé la base actuelle ; le serveur
redémarre et vérifie qu’il répond. Menu → **Essais à faire** liste ce qui n’a
pas encore été essayé en vrai, avec ses étapes et votre compte rendu.

## L’explorateur et la règle des fichiers du PC

Tout ce qui touche aux fichiers passe par l’explorateur de l’appli, jamais par un chemin
tapé : les dossiers de travail, les coffres, les dossiers autorisés et les sauvegardes, un
fichier pour la mémoire d’un projet, une pièce jointe, un import, une conversation
enregistrée sur le PC. Une seule règle les juge (`aihub/places.py`) :

- un disque local du serveur seulement : un chemin réseau n’est jamais résolu ;
- dans les **dossiers autorisés** des réglages quand il y en a (sinon tous les disques
  locaux), les liens étant jugés là où ils mènent ;
- lire un fichier du PC : le propriétaire et les administrateurs ; y créer un dossier, un
  coffre ou un fichier demande en plus votre mot de passe AI Hub ;
- les membres et les invités ne lisent ni n’écrivent rien sur le PC : leurs fichiers
  viennent de leur appareil (« Depuis cet appareil… »).

Seule la fenêtre des réglages, au propriétaire, choisit ses dossiers sur tous les disques
locaux : c’est là que les dossiers autorisés se définissent.

L’explorateur part des disques et des dossiers habituels (ou des dossiers autorisés), puis
un clic entre dans un dossier ; il marche depuis un téléphone ou un deuxième PC, où la
fenêtre de Windows n’existe pas. Il ne montre que des noms et ne modifie rien en
parcourant ; les dossiers cachés et les liens qui sortent du dossier ne sont pas montrés.
Pour un espace isolé, **Choisir dans le dossier de travail…** coche les fichiers et
dossiers à copier.

## Coffres Obsidian

Le menu **Coffres Obsidian** (propriétaire) liste les coffres du PC serveur :
ceux créés par AI Hub, ceux qu’Obsidian connaît et ceux trouvés sur les disques
(un dossier qui contient `.obsidian`), même créés autrement. **Ouvrir** affiche
les notes du coffre, avec une recherche et les liens `[[…]]`, aussi depuis un
téléphone. **Utiliser pour les notes** en fait le coffre des notes de projet,
après enregistrement des réglages.

La recherche sur les disques est bornée (dossier personnel, premiers niveaux de
chaque disque, voisins des coffres connus) et saute les dossiers cachés et
système. Tout se fait en lecture seule ; rien d’un coffre n’est envoyé à une IA.

## Installation Windows

1. Téléchargez `ai-hub-1.3.0.zip` dans les
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
2. Téléchargez et extrayez `ai-hub-1.3.0-linux.tar.gz`.
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

AI Hub cherche Claude Code, Codex et Docker Sandboxes à chaque lancement, et
non une fois pour toutes au démarrage : le PATH, puis les emplacements de leurs
installeurs (applications Claude et Codex, installeur officiel, npm, winget,
extensions d’éditeur ; `~/.local/bin`, `/usr/local/bin`… sous Linux), la copie
la plus récente d’abord. Une mise à jour qui déplace la CLI ne casse donc plus
rien. Un programme installé ailleurs se trouve avec **Réglages → IA proposées
et sauvegardes → Chercher sur ce PC** (une minute au plus, disques locaux
seulement), qui permet de **Retenir** une copie.
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

**Réglages → Vérifier le fonctionnement** vérifie la base, le disque, la présence de Claude Code
et de Codex, le moteur d’isolation et le compte de l’espace choisi, sans envoyer de demande à une IA. Une action précise
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
