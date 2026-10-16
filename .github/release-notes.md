AI Hub 1.2.3 fait tout passer par l’explorateur de l’appli, sous une seule règle pour les fichiers du PC.

- **Plus de chemin à taper** : dossiers de travail, coffres, dossiers autorisés et sauvegardes
  se choisissent dans l’explorateur, qui crée aussi un dossier ou un coffre sur place.
- **Depuis le PC** : un fichier pour la mémoire d’un projet, une pièce jointe, un import, et
  « Exporter → Enregistrer sur le PC… ». « Depuis cet appareil… » reste pour le téléphone.
- **Une seule règle** : un disque local du serveur (jamais un chemin réseau), dans les dossiers
  autorisés des réglages quand il y en a ; le mot de passe pour créer ; rien pour les membres
  et les invités. Les coffres la suivent aussi.
- Avec la 1.2.2 : « Relier à Obsidian… », un nouveau coffre ou un coffre existant, un coffre par conversation.

Windows : `ai-hub-1.2.3.zip`, puis `installer.bat`.
Linux : `ai-hub-1.2.3-linux.tar.gz`, puis `sh installer.sh` et `./lancer.sh`.
Python 3.12 ou plus est nécessaire. Les espaces isolés nécessitent Docker Sandboxes.
La base passe au schéma 29 (depuis la 1.2.1) ; conservez `data/` et une sauvegarde avant la mise à jour.
Les empreintes des paquets sont dans `SHA256SUMS`.
