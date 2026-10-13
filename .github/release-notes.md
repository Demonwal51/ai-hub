AI Hub permet de reprendre les conversations et plans interrompus.

- **Reprendre** apparaît sous la dernière réponse arrêtée ou en erreur.
- **Reprendre le plan** poursuit seulement les tâches inachevées d’un plan
  déjà validé. Les étapes terminées et leurs résultats sont conservés.
- Si seule la synthèse est interrompue, les tâches ne sont pas rejouées.
- Le texte partiel et la session sont sauvegardés pendant le travail. Les
  points de reprise restent après redémarrage du serveur.
- Les sessions restent liées au compte et au contexte d’accès. Une session
  perdue entraîne un nouveau départ avec le point sauvegardé, sans copier les
  identifiants. Les accès et permissions actuels restent imposés.
- La reprise est manuelle ; un espace suspendu doit d’abord être réactivé.
  Un plan annulé avant sa validation ne peut pas être exécuté par cette action.

La reprise ne restaure pas une commande externe à l’instruction près : l’IA
doit vérifier ce qui est déjà fait avant de répéter une action. Un plan commencé
se reprend dans son dossier de travail initial.

Windows : `ai-hub-1.0.8.zip`, puis `installer.bat`.
Linux : `ai-hub-1.0.8-linux.tar.gz`, puis `sh installer.sh` et `./lancer.sh`.
Python3.12 ou plus est nécessaire. Les espaces isolés nécessitent Docker Sandboxes.
La base passe au schéma26 ; conservez `data/` et une sauvegarde avant mise à jour.
Les empreintes des paquets sont dans `SHA256SUMS`.
