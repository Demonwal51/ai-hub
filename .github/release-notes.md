Cette version permet de créer un nouveau coffre Obsidian depuis AI Hub.

- Depuis **Obsidian → Créer un nouveau coffre…**, choisissez le chemin complet
  d’un nouveau dossier sur le PC serveur. Le bouton est aussi dans **Réglages**.
- Le coffre et son dossier `.obsidian` sont créés sans installer Obsidian.
- Enregistrez ensuite les réglages pour utiliser ce coffre. Les anciennes notes
  restent dans leur ancien dossier ; elles ne sont pas déplacées.
- La création est réservée au propriétaire et respecte les dossiers autorisés.
  Un dossier déjà existant est refusé, ainsi que les chemins passant par un lien.

Windows : `ai-hub-1.0.5.zip`, puis `installer.bat`.
Linux : `ai-hub-1.0.5-linux.tar.gz`, puis `sh installer.sh` et `./lancer.sh`.
Python3.12 ou plus est nécessaire. Les espaces isolés nécessitent Docker Sandboxes.

AI Hub utilise toujours un seul coffre configuré pour ses notes de projet.

Schéma24 inchangé ; conservez `data/` et une sauvegarde avant mise à jour.
Les empreintes des paquets sont dans `SHA256SUMS`.
