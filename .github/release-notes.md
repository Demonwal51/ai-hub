AI Hub réunit Claude Code, ChatGPT via Codex et Ollama dans une interface en
français, avec des conversations, projets et accès choisis.

Cette version apporte les espaces isolés et les copies de fichiers à vérifier
avant application, les comptes séparés par personne/espace/fournisseur, les
trois profils d’accès, le bouton **Tout couper**, un guide de démarrage et un
diagnostic intégré. Elle améliore aussi l’organisation des conversations,
l’interface mobile, les sauvegardes avec pièces jointes et le retour après
échec de démarrage d’une mise à jour.

### Installation

- **Windows** : extrayez `ai-hub-1.0.1.zip`, puis lancez `installer.bat`.
- **Linux** : extrayez `ai-hub-1.0.1-linux.tar.gz`, puis lancez
  `sh installer.sh` et `./lancer.sh`.
- Python 3.12 ou plus est nécessaire. Les espaces isolés nécessitent Docker
  Sandboxes et une connexion à votre fournisseur IA dans chaque espace.

Les suites de 684 tests sont validées sous Windows et Ubuntu. L’application
et ses paquets ont également été vérifiés sur Debian13 et Fedora44, avec
63 tests ciblés sur chaque distribution. Le moteur Docker Sandboxes natif
Linux avec KVM reste à valider séparément.

### Avant de mettre à jour

Conservez votre dossier `data/` et une sauvegarde cohérente de la base.
La migration passe au **schéma24**. Les anciennes versions du code ne peuvent
pas ouvrir ce nouveau schéma. Le retour de mise à jour rétablit le code et
la base, mais pas les dépendances Python.

Le fichier `SHA256SUMS` permet de vérifier les empreintes des deux paquets.
