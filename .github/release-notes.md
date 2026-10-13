Cette version rétablit l’écran de connexion après une interruption du serveur.

- Le bouton ne reste plus bloqué après une première erreur réseau.
- « Réessayer la connexion » et une nouvelle vérification automatique permettent
  de reprendre lorsque le serveur ou Tailscale redevient disponible.
- Les requêtes de connexion attendent au maximum 15 secondes la réponse initiale.
- Aucun mot de passe n’est envoyé automatiquement lors des nouvelles vérifications.
  La protection contre les tentatives répétées reste active.

Windows : `ai-hub-1.0.4.zip`, puis `installer.bat`.
Linux : `ai-hub-1.0.4-linux.tar.gz`, puis `sh installer.sh` et `./lancer.sh`.
Python3.12 ou plus est nécessaire. Les espaces isolés nécessitent Docker Sandboxes.

Les deux appareils doivent toujours être connectés au même réseau Tailscale
pour accéder au serveur privé. Cette correction ne rétablit pas une connexion
Tailscale désactivée sur un appareil.

Schéma24 inchangé ; conservez `data/` et une sauvegarde avant mise à jour.
Les empreintes des paquets sont dans `SHA256SUMS`.
