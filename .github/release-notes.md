AI Hub 1.2.1 ne montre que l’essentiel, comme Claude Code, et dit quand une mise à jour est à faire.

- **L’écran** : la liste à gauche, la conversation au centre, une seule zone de saisie
  en bas (📎, l’IA, ce qu’elle peut faire sans demander, ↑). L’accueil : la demande,
  le projet, l’IA.
- **Des menus courts** : ⋯ (renommer, archiver, projet, consignes, exporter,
  supprimer) et Réglages (quota, apparence, notifications, mises à jour, IA proposées).
- **« Fonctions avancées »** (Réglages) remet le reste à l’écran : plusieurs IA, travail
  autonome, comparer, planifier, espaces isolés, partage, coffres…
- **Mises à jour détectées** : « ⬆ Mise à jour à faire » et la fenêtre « Mises à jour ».
  Une version plus récente sur GitHub s’installe en un clic (installation .zip) ; le code
  changé sur le PC (copie git) se met en service par « Redémarrer sur le nouveau code ».
- **Sans dossier**, une conversation reçoit un dossier vide rien qu’à elle.

Windows : `ai-hub-1.2.1.zip`, puis `installer.bat`.
Linux : `ai-hub-1.2.1-linux.tar.gz`, puis `sh installer.sh` et `./lancer.sh`.
Python 3.12 ou plus est nécessaire. Les espaces isolés nécessitent Docker Sandboxes.
La base reste au schéma 28 ; conservez `data/` et une sauvegarde avant la mise à jour.
Les empreintes des paquets sont dans `SHA256SUMS`.
