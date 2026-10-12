Cette version permet de créer les dossiers et fichiers directement depuis AI Hub.

### Nouveautés

- **Créer ce dossier sur le serveur** dans les formulaires de conversation
  et de projet : le dossier de travail peut être préparé avant leur création.
- **Créer un fichier ou un dossier…** dans le menu d’une conversation :
  création d’un fichier vide ou d’un sous-dossier, avec parents manquants si choisi.
- Respect des emplacements autorisés, vérification du mot de passe et refus
  d’écraser un fichier existant ou de passer par un lien ou une jonction.
- Création réservée au propriétaire et aux administrateurs sur le PC serveur ;
  les permissions de l’IA ne sont pas élargies par cette action manuelle.

### Installation

- Windows : extrayez `ai-hub-1.0.2.zip`, puis lancez `installer.bat`.
- Linux : extrayez `ai-hub-1.0.2-linux.tar.gz`, puis lancez `sh installer.sh`
  et `./lancer.sh`.
- Python3.12 ou plus est nécessaire. Les espaces isolés nécessitent Docker
  Sandboxes et une connexion à votre fournisseur IA dans chaque espace.

Conservez votre dossier `data/` et une sauvegarde de la base avant mise à jour.
Le schéma reste en version24. Le moteur Docker Sandboxes natif Linux avec KVM
reste à valider séparément ; cela ne change pas la compatibilité Linux de l’application.

Les fichiers `SHA256SUMS` permettent de vérifier les empreintes des paquets.
