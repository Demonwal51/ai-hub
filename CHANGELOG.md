# Journal des versions

## 1.2.3 — 16 octobre 2026 : l’explorateur partout, une seule règle pour les fichiers du PC

- **L’explorateur de l’appli pour tout ce qui touche aux fichiers** : plus aucun chemin à
  taper. Les dossiers de travail, du projet, de l’import, des sauvegardes, les coffres et
  les dossiers autorisés se choisissent dans l’explorateur (« Choisir… », « Vider »).
  On y crée aussi un dossier (« ＋ Nouveau dossier ») ou un coffre, nommé sur place.
- **Depuis le PC, par l’explorateur** (le propriétaire et les administrateurs) :
  - « Ajouter un fichier… » à la mémoire d’un projet (Markdown, texte, CSV, JSON, YAML) ;
  - 📎 un fichier du PC à la prochaine demande ;
  - l’import d’un export ChatGPT ou Claude, ou d’un projet AI Hub (.zip) ;
  - « Exporter » propose **Enregistrer sur le PC…** : la conversation en Markdown, dans
    le dossier choisi, comme nouveau fichier (« (2) »… si le nom est pris).
  « Depuis cet appareil… » reste là pour un fichier du téléphone ou de l’autre PC.
- **Une seule règle** (`aihub/places.py`) juge chaque fichier et chaque dossier : un disque
  local du serveur (un chemin réseau n’est jamais résolu, aussi dans les réglages), dans
  les dossiers autorisés des réglages quand il y en a, liens jugés là où ils mènent. Les
  coffres (d’un projet, d’une conversation, lus dans « Coffres Obsidian ») la suivent aussi.
  Lire un fichier du PC : le propriétaire et les administrateurs ; y créer quelque chose
  demande en plus le mot de passe. Les membres et les invités ne touchent pas au PC.
- Un dossier immense (un Temp) s’ouvre vite : il est lu en partie, et l’explorateur le dit.

## 1.2.2 — 16 octobre 2026 : relier une conversation à Obsidian

- **« Relier à Obsidian… »** revient dans le menu ⋯ d’une conversation. On choisit
  **Créer un nouveau coffre** (un dossier neuf, prêt à ouvrir dans Obsidian, proposé
  à côté des coffres connus) ou **Choisir un coffre existant** (les coffres trouvés
  sur le PC, ou un autre dossier). La note de la conversation se fait dans ce coffre ;
  une note qui existe déjà n’est jamais reprise sans le demander.
- Une fois reliée : le coffre et la note sont dits, avec **Changer de coffre** et
  **Délier**. « Noter dans Obsidian » sous une réponse l’y ajoute.
- **Un coffre par conversation** (schéma 29) : celui choisi en la reliant passe avant
  celui de son projet, puis celui des réglages. Seul le propriétaire du serveur le
  choisit.

## 1.2.1 — 16 octobre 2026 : l’essentiel seulement, comme Claude Code

- **À gauche** : « Nouvelle conversation », la recherche, la liste, « Réglages ».
  Les archives sont au bout de la liste. L’état des IA et le quota n’apparaissent
  que s’il y a un problème (une IA absente, un quota presque épuisé).
- **L’accueil** : la demande, le projet (« ＋ Nouveau projet… » dans la liste),
  l’IA, la flèche ↑.
- **Une conversation** : le titre et ⋯ en haut ; en bas une seule zone de saisie
  avec 📎, l’IA, ce qu’elle peut faire sans demander, et ↑. Le menu ⋯ : Renommer,
  Archiver, Projet, Consignes, Exporter, Supprimer. Sous une réponse : Copier,
  Régénérer.
- **Réglages** : Quota des IA, Apparence, Notifications, Mises à jour, IA proposées
  et sauvegardes, Bien démarrer. La fenêtre d’un projet : nom, dossier, consignes.
- **« Fonctions avancées »** (Réglages, sur chaque appareil, éteint d’office) remet
  tout le reste à l’écran : plusieurs IA, travail autonome, comparer, favoris,
  citer, planifier, espaces isolés, outils, mémoire, partage, coffres, personnes,
  sauvegardes, import… Rien n’est supprimé : les conversations qui s’en servent
  marchent comme avant.

## 1.2.0 — 16 octobre 2026 : plus simple à utiliser, et les mises à jour détectées

- **L’accueil, c’est la demande** : on écrit ce qu’on veut, on choisit l’IA (et
  un projet si on veut), puis Envoyer. La conversation se crée toute seule, nommée
  par le début de la demande. « Reprendre » montre les quatre dernières
  conversations ; « Plus d’options… » ouvre tous les choix.
- **Un dossier automatique** : sans dossier choisi (ni dossier du projet), la
  conversation reçoit un dossier vide rien qu’à elle sur le PC serveur, au lieu
  d’un refus. Un dossier choisi reste vérifié comme avant.
- **« Réglages » remplace le long menu** : trois groupes (Pour vous, Le serveur,
  Aide), chaque entrée avec une phrase qui dit à quoi elle sert.
- **Le menu ⋯ d’une conversation est rangé** : Conversation, L’IA, Espace et
  fichiers, Plus. « Accès de cette IA » y entre.
- **La zone de saisie s’allège** : un bouton ＋ pour joindre un fichier, les
  demandes favorites, citer une conversation, le travail autonome et comparer
  avec une autre IA (dite au-dessus de la saisie tant qu’elle vaut).
- **La fenêtre « Nouvelle conversation »** garde l’essentiel (modèle de travail,
  titre facultatif, projet, IA) ; dossier, plusieurs IA, espace isolé et
  permissions passent dans « Options avancées ». Les réglages de l’appli font de
  même avec leurs « Réglages avancés ».
- **Des noms clairs** : « Claude · Sonnet », « ChatGPT · GPT-6 Sol » ;
  « Demande avant d’agir », « Propose un plan d’abord », « Agit sans demander ».
- **« Bien démarrer » en trois pas** : vérifier ses IA, écrire sa demande, ranger
  avec les projets ; le reste est dit dans « Pour aller plus loin ».
- **Mises à jour détectées** : « ⬆ Mise à jour à faire » dans la barre de gauche,
  et la fenêtre « Mises à jour » (Réglages). Elle voit une version plus récente
  publiée sur GitHub (installée en un clic depuis une installation .zip), et aussi
  le code changé sur ce PC depuis le démarrage du serveur (une copie git) :
  « Redémarrer sur le nouveau code » l’essaie d’abord à part (il se charge, il
  ouvre une copie de la base), refuse si l’essai échoue, si une IA travaille ou
  si des fichiers sont encore en train de changer, copie la base si son schéma
  change, puis redémarre et vérifie le retour.
- **Une page à jour** : une page ouverte (le téléphone, par exemple) dit quand le
  serveur tourne sur une autre version, avec « Recharger la page ».

## 1.1.1 — 16 octobre 2026 : passer la suite d’un plan sur une autre IA

- **« Changer d’IA pour la suite… »** sous un plan validé, en cours ou
  interrompu : quand une IA n’a plus de quota (ChatGPT par exemple), toutes les
  tâches pas encore faites passent à celle que vous choisissez (Claude), et le
  chef aussi si vous le cochez, puisqu’il écrit la synthèse.
- Les tâches en cours ou terminées gardent leur IA ; un plan interrompu reprend
  aussitôt. Le contenu du plan reste celui validé : seule l’IA de la suite change.
- Schéma 28 : le gel d’un plan s’ouvre pour ce seul changement.

## 1.1.0 — 16 octobre 2026 : sept fonctions

- **Un coffre par projet** : « Coffre des notes de ce projet » dans la fenêtre d’un
  projet (le propriétaire). Création, plans, réponses notées et réécriture vont
  dans ce coffre ; vide, celui des réglages.
- **Un plan qui ne s’arrête pas sur un quota** : « Reprendre automatiquement à … »
  (l’heure de retour du quota, plus une marge) ou « Confier à cette IA et
  reprendre ». Les tâches terminées ne sont pas rejouées ; la reprise programmée
  s’annule, et un refus est dit sur le plan avec une alerte.
- **Une note par conversation** : une note qui existe déjà n’est plus reprise sans
  le demander : la continuer, ou en créer une nouvelle (« Titre (2) »).
- **Des coffres plus utiles** : « Chercher dans le texte » de toutes les notes, et
  « Ajouter à un projet… » : la note rejoint la mémoire RAG du projet et se met à
  jour quand elle change dans Obsidian (sa dernière copie reste si elle disparaît).
- **Restaurer une sauvegarde** depuis « Serveur » : mot de passe, contrôle de la
  sauvegarde, base actuelle gardée, redémarrage vérifié, ancienne base remise si
  le serveur ne revient pas.
- **Emporter un projet entier** : « Exporter le projet (.zip) » (conversations,
  consignes, accès, documents, notes) et « Importer → Un projet AI Hub (.zip) »,
  tout ou rien, sans jamais écraser une note.
- **« Essais à faire »** : ce qui n’a jamais été essayé en vrai, étape par étape,
  avec « Ça marche » / « Problème » et une remarque.
- Schéma 27 (migration automatique ; une sauvegarde est faite avant).

## 1.0.10 — 16 octobre 2026 : choisir les dossiers dans l’appli

- **Parcourir…** à côté de chaque chemin du PC serveur : dossier de travail
  d’une conversation, d’un projet ou d’un import, dossiers autorisés, coffre
  des notes, sauvegardes, stockage du moteur isolé, et création d’un dossier
  ou d’un coffre. Plus besoin de la fenêtre de Windows ni de taper le chemin,
  et cela marche depuis un téléphone ou un deuxième PC.
- Le sélecteur part des disques et des dossiers habituels, entre dans un
  dossier d’un clic, remonte, accepte un chemin tapé, signale les coffres et
  peut créer un nouveau dossier sur place.
- **Choisir dans le dossier de travail…** pour un espace isolé : les fichiers
  et dossiers se cochent au lieu de s’écrire un par ligne.
- **Ouvrir un dossier…** dans « Coffres Obsidian » : ouvre n’importe quel coffre
  du PC, même absent de la liste.
- Mêmes limites qu’avant : dossiers autorisés pour les dossiers de travail
  (propriétaire et administrateurs), réglages réservés au propriétaire,
  dossiers cachés non montrés, liens qui sortent du dossier ignorés. On ne
  voit que des noms : aucun fichier n’est lu.

## 1.0.9 — 15 octobre 2026 : coffres Obsidian et deux corrections

- Fenêtre « Coffres Obsidian » (menu, propriétaire) : les coffres créés par AI Hub,
  ceux qu’Obsidian connaît et ceux trouvés sur les disques du PC (un dossier qui
  contient `.obsidian`). Chaque coffre s’ouvre pour lire ses notes, avec la
  recherche et les liens `[[…]]` ; sur téléphone, la liste puis la note.
- « Utiliser pour les notes » et « Choisir parmi les coffres du PC… » dans les
  réglages : le coffre des notes de projet se choisit dans la liste, puis
  s’enregistre avec le mot de passe comme les autres réglages.
- Lecture seule : rien d’un coffre n’est modifié ni envoyé à une IA ; les
  dossiers cachés (`.obsidian`, `.trash`) ne s’ouvrent pas.
- Correction : une réponse ne va plus deux fois dans la note de projet ; la
  synthèse d’un plan y va avec son plan, et « Réécrire la note » ne la copie
  plus en double.
- Correction : un plan demandé sans prompt puis validé n’est plus présenté par
  le chef comme « un plan seulement » ; agents et synthèse savent que le plan
  a été validé avant son exécution.

## 1.0.8 — 13 octobre 2026 : reprise des travaux interrompus

- « Reprendre » sous la dernière réponse interrompue et « Reprendre le plan »
  pour les plans validés : les tâches terminées ne sont pas rejouées.
- Points de reprise et sessions sauvegardés pendant le travail, conservés après
  un redémarrage. Synthèse reprise seule lorsque les tâches sont déjà terminées.
- Sessions liées au compte et au contexte d’accès ; repli sur le texte sauvegardé
  si la session a disparu. Pièces jointes originales et erreurs antérieures conservées.
- Réactivation explicite nécessaire pour un espace suspendu ; pas de reprise
  automatique. Schéma26, avec migration des données existantes.

## 1.0.7 — 13 octobre 2026 : demander un plan sans prompt

- « Demander un plan » fonctionne avec un champ vide. Le chef reçoit un
  contexte borné des échanges récents et du résumé existant de la conversation.
- Le texte reste facultatif : il permet de préciser la nouvelle demande.
  Sans objectif connu, le chef est invité à proposer une clarification ou une
  analyse initiale. Aucun travail n’est lancé avant validation du plan.
- Une discussion ou un message à une seule IA requiert toujours un texte.

## 1.0.6 — 13 octobre 2026 : mémoire RAG de projet

- Panneau « Mémoire RAG… » : recherche dans les conversations autorisées,
  association explicite de documents texte et de notes au projet, aperçu des
  passages et retrait des sources des recherches futures.
- Documents découpés et indexés dans SQLite ; recherche par mots et, quand le
  modèle local est disponible, par le sens. Aucun parcours automatique du coffre.
- Passages sourcés transmis aux IA, au chef d’orchestre, aux sous-tâches et aux
  réponses comparées. Les contenus sont présentés comme des références.
- Respect des partages, des révocations et du périmètre mémoire des espaces
  isolés. Le schéma passe à 25 ; les documents font partie des sauvegardes SQLite.

## 1.0.5 — 13 octobre 2026 : créer un coffre Obsidian

- Création d’un nouveau coffre depuis Obsidian ou Réglages, sur le PC serveur,
  avec son dossier `.obsidian` et création facultative des dossiers parents.
- Le chemin est proposé dans les réglages ; l’utilisateur les enregistre pour
  utiliser ce coffre. Les anciennes notes ne sont pas déplacées.
- Création réservée au propriétaire, vérification du mot de passe, respect des
  dossiers autorisés et refus des chemins existants ou passant par un lien.

## 1.0.4 — 13 octobre 2026 : connexion après une interruption

- L’écran de connexion peut réessayer quand le serveur revient, au lieu de
  conserver « Se connecter » désactivé après une première erreur réseau.
- Bouton « Réessayer la connexion », reprise automatique et délai maximal
  de 15 secondes pour obtenir une réponse aux requêtes de connexion.
- Aucune retransmission automatique des mots de passe ; la protection contre
  les tentatives répétées reste active.

## 1.0.3 — 13 octobre 2026 : reprendre après un plan annulé ou refusé

- Annuler un plan d’orchestration libère immédiatement la saisie, même si
  l’actualisation suivante échoue ; les autres appareils ouverts sont avertis.
- Refuser le plan proposé par Claude termine la requête en attente et libère
  la conversation pour une nouvelle demande, sans autoriser le plan.
- Les demandes d’autorisation encore en mémoire de la requête arrêtée sont
  ignorées ; les permissions choisies pour la conversation restent inchangées.
- Message explicite après le refus et confirmation d’annulation plus claire :
  « Annuler le plan » ou « Conserver le plan ».

## 1.0.2 — 12 octobre 2026 : création de fichiers et dossiers

- Création du dossier de travail depuis les formulaires de conversation et
  de projet, sans passer par l’explorateur du PC serveur.
- Création d’un fichier vide ou d’un sous-dossier depuis le menu d’une
  conversation, avec création facultative des parents manquants.
- Opérations réservées au propriétaire et aux administrateurs, avec
  vérification de leur mot de passe lorsque l’authentification est active.
- Respect des dossiers autorisés et du périmètre de la conversation ; refus
  des liens, jonctions, remontées de chemin et écrasements de fichiers existants.
- La création manuelle ne modifie pas les permissions accordées à l’IA.

## 1.0.1 — 12 octobre 2026 : accès guidés et diagnostic

- Trois profils d’accès personnalisables : discussion sans fichiers du PC,
  analyse de copies sélectionnées et travail sur un projet dans une copie à
  vérifier avant application.
- Panneau des accès et suspension durable avec réactivation réservée au
  propriétaire. La suspension conserve l’héritage du projet et les comptes IA.
- Annulation des connexions en attente et arrêt demandé aux travaux en cours
  lors d’une suspension. Les nouveaux lancements restent refusés jusqu’à la
  réactivation.
- Guide de premier espace, bouton direct pour revoir les résultats et
  compteurs des fichiers créés, modifiés ou supprimés. Une conversation
  suspendue explique pourquoi la saisie est bloquée.
- Diagnostic de la base, du disque, du moteur d’isolation et du compte de
  l’espace, avec actions proposées et sans envoyer de message à une IA.

## 1.0.0 — 12 octobre 2026 : intégrée dans la publication 1.0.1

- Conversations et projets isolés avec des accès distincts aux fichiers, au
  réseau et à la mémoire. Les limites d’un projet s’appliquent aussi aux
  conversations déjà créées.
- Exécution dans les microVMs Docker Sandboxes et connexion séparée par
  personne et espace. Aucun repli automatique sur le PC, outil MCP de l’hôte
  ni transmission de ses identifiants IA dans l’espace isolé.
- Copies des fichiers sélectionnés pour lecture ou modification. Revue,
  téléchargement et application explicite des résultats, avec contrôle des
  conflits et sauvegarde des originaux.
- Inférence Ollama conservée sur le service local en boucle locale, avec
  outils de fichiers en lecture seule dans la VM.
- Exécution isolée obligatoire pour les membres et invités ; arrêt des travaux
  des comptes révoqués. Réutiliser un numéro de compte ou de conversation
  supprimé ne permet pas de récupérer une ancienne VM connectée.
- Installation, lanceurs et démarrage automatique sous Linux ; permissions
  exécutables conservées dans le ZIP et paquet Linux tar.gz supplémentaire.
  Installation de l’application testée sur Debian 13, Fedora 44 et Ubuntu 26.04.
- Interface plus lisible, thèmes et densité par appareil, formulaires mobiles
  et respect des préférences de réduction des animations. Bouton direct des
  comptes et liens ouvrant le bon fournisseur dans l’espace choisi.
- Claude utilise l’image de sa CLI officielle dans un profil shell neutre :
  sa connexion OAuth reste dans sa VM et n’est pas capturée comme identifiant
  Docker global partagé. Les identifiants globaux restent refusés.
- Les installations réservées au PC refusent aussi les écritures depuis un
  autre site et les en-têtes Host inattendus. Une réponse de session retardée
  ne peut pas remplacer le cache d’un autre compte.
- Contrôles de révocation pendant les flux et de l’identité des copies hors
  connexion, y compris lorsqu’une ancienne réponse arrive après un changement
  de compte.
- Sauvegardes quotidiennes avec pièces jointes et empreintes. Restauration
  préparée et vérifiée avant remplacement. Mise à jour avec sauvegarde de la
  base, contrôle du nouveau serveur et retour après échec de démarrage.

Les connexions humaines et réponses réelles de Claude et Codex ont été
vérifiées dans des microVMs distinctes sur un hôte Windows. Le moteur natif
Linux avec KVM reste à valider séparément. Le retour de mise à jour ne restaure
pas les dépendances Python modifiées.

## 0.6.0 bêta — intégrée dans la publication 1.0.1

- Épingles et archives personnelles, sans modifier la liste des autres
  participants d’une conversation partagée.
- Filtres de recherche par projet, IA, date et archives. Permissions et
  filtres appliqués avant le classement, pour les recherches par mots ou par sens.
- Modèles de travail avec champs à remplir et modèles personnels enregistrés
  depuis les réglages et le brouillon d’une conversation. Leur création
  n’envoie jamais automatiquement la demande.
- Rendu enrichi des réponses : titres, tableaux, listes, citations, liens sûrs
  et copie du code. Position de lecture conservée pendant l’arrivée de la réponse.
- Correction des résumés manuels qui utilisaient les comptes du serveur pour
  les invités. Choix des modèles selon le compte et prise en compte des accès
  de l’expéditeur dans la synthèse d’orchestration.
- Branches conservant les consignes, outils et copies indépendantes des pièces.
- Récupération des comparaisons et délégations interrompues et de l’état des
  tâches planifiées. Routage explicite en lecture seule si seule une IA locale
  est disponible.

## 0.5.0 bêta — 11 octobre 2026

- Régénérer la dernière réponse avec la même IA ou une autre ; modifier une
  demande dans une nouvelle conversation créée à partir de ce point, une branche.
- Comparer deux IA : une seconde répond en lecture seule à la même demande,
  à côté de la première réponse, avec l’action « Garder celle-ci ».
- Consignes par conversation et projet, transmises à chaque demande.
- Lecture à voix haute et raccourcis clavier : Ctrl+K, Ctrl+Maj+N, Échap et ?.
- Tableau de bord du serveur pour son propriétaire et lecture sans connexion
  au PC sur les appareils qui ont choisi de conserver une copie.

## 0.4.0 bêta — intégrée dans la publication 0.5

- Fichiers et images joints à une demande : pièce jointe, capture collée ou
  fichier déposé. Texte intégré à la demande, images transmises aux IA qui les
  prennent en charge, pièces affichées sous la demande.
- Dictée et demandes favorites conservées sur le serveur pour chaque personne.
- Export de conversation en Markdown, JSON réimportable dans AI Hub ou page
  à imprimer et enregistrer en PDF.
- Consommation par personne et IA pour le propriétaire et les administrateurs.
- Mise à jour depuis les versions GitHub, avec conservation de l’ancienne
  version et retour en cas d’échec.
- Installation par ZIP et `installer.bat`, avec raccourci sur le Bureau.

## 0.3.0 bêta — 11 octobre 2026

- Création d’un serveur depuis sa page sur son propre PC : nom du propriétaire
  et deux mots de passe facultatifs, pour le compte et pour le serveur.
- Comptes sans mot de passe et mot de passe du serveur demandé une fois par
  appareil. Ajout d’une personne par compte créé ou lien d’invitation.
- Rôles invité, membre et administrateur, avec accès par projet ou conversation
  en lecture ou écriture, choisis lors de l’ajout de la personne.
- Connexion d’un téléphone à un compte par QR code valable 15 minutes ; nommage
  des appareils dans « Appareils ».
- Connexion de Claude ou ChatGPT guidée au premier accès ; connexion des comptes
  du PC depuis l’interface pour le propriétaire.
- Import des exports ChatGPT et Claude et des sessions locales Claude Code et
  Codex, séparément ou fusionnés dans une conversation.
- Nouvelle présentation : liste des conversations, quota sur une ligne, menus,
  Entrée pour envoyer, brouillons, copie, marqueurs de nouveauté et chargement
  progressif des longues conversations.

## 0.2.0 bêta — 11 octobre 2026

Plusieurs personnes sur un serveur, chacune avec ses propres comptes IA.

- Invitations par lien à usage unique valable sept jours ; connexion par nom
  et mot de passe.
- Partage d’une conversation ou d’un projet en lecture ou lecture et écriture.
  Les contenus non partagés répondent « introuvable » ; les réglages réservés
  au propriétaire le restent.
- Comptes Claude et Codex propres à chaque personne dans « Mes comptes IA ».
  Ses demandes, plans, délégations et quotas utilisent ses comptes.
- Auteur indiqué sur chaque demande ; permission validée uniquement par son
  expéditeur ; notifications limitées aux contenus partagés et quotas personnels.
- Création directe de comptes et modification des noms par le propriétaire.

## 0.1.0 bêta — 10 octobre 2026

Première version publiée.

- Fournisseurs Claude Code, Codex et modèles locaux Ollama. Trois modes de
  permission : manuel, plan et actions sans demande, avec confirmation préalable.
- Conversations avec une IA ou orchestration : le chef prépare le plan,
  les agents réalisent les tâches indépendantes en parallèle sans modifier
  simultanément les mêmes fichiers, puis le chef produit la synthèse.
- Affectation automatique selon les évaluations disponibles et changement d’IA
  à partir de la mémoire de la conversation.
- Mémoire et recherche par mots avec SQLite FTS5 ou par sens avec des vecteurs
  locaux ; nouvelle session avec un contexte limité en tokens.
- Projets dont les conversations peuvent se consulter via des outils en lecture seule.
- Travail autonome avec commande de vérification, tâches planifiées, outils
  par conversation, recherche web, coffre Obsidian, serveurs MCP et délégation.
- Interface installable sur téléphone, accès Tailscale, mots de passe hachés,
  sessions par appareil, protection contre les essais répétés et notifications.
- Sauvegardes quotidiennes, suppression définitive aussi dans les sauvegardes,
  fenêtre de réglages et notes de projet dans un coffre Obsidian.
