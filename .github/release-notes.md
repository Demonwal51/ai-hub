AI Hub 1.2.4 rend la mise à jour en un clic plus sûre et plus rapide.

- **L’empreinte vérifiée** : le paquet téléchargé doit avoir l’empreinte publiée dans
  `SHA256SUMS`, sinon la mise à jour est refusée.
- **20 secondes de moins** : le serveur s’arrête dès qu’il le peut avant une mise à jour.
- **Un retour en arrière plus solide** : il n’utilise plus le code qu’il défait et remet les
  anciennes dépendances quand elles diffèrent ; `data/mise-a-jour/journal.txt` dit pourquoi.
- Ces protections valent pour les mises à jour faites depuis la 1.2.4.
- Avec la 1.2.3 : l’explorateur de l’appli pour tout ce qui touche aux fichiers, sous une seule règle.

Windows : `ai-hub-1.2.4.zip`, puis `installer.bat`.
Linux : `ai-hub-1.2.4-linux.tar.gz`, puis `sh installer.sh` et `./lancer.sh`.
Python 3.12 ou plus est nécessaire. Les espaces isolés nécessitent Docker Sandboxes.
La base reste au schéma 29 (depuis la 1.2.1) ; conservez `data/` et une sauvegarde avant la mise à jour.
Les empreintes des paquets sont dans `SHA256SUMS`.
