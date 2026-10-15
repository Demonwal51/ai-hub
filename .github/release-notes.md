AI Hub ouvre les coffres Obsidian du PC et corrige deux défauts des notes et des plans.

- **Coffres Obsidian** (menu, propriétaire) : les coffres créés par AI Hub,
  ceux qu’Obsidian connaît et ceux trouvés sur les disques, même créés autrement.
- **Ouvrir** lit les notes d’un coffre dans l’appli, avec une recherche et les
  liens `[[…]]`, aussi sur téléphone. Lecture seule ; rien n’est envoyé à une IA.
- **Utiliser pour les notes** choisit le coffre des notes de projet ; les
  réglages proposent aussi « Choisir parmi les coffres du PC… ».
- Une réponse ne va plus deux fois dans la note de projet, et la synthèse d’un
  plan n’y est plus copiée en double.
- Un plan demandé sans prompt puis validé n’est plus présenté comme « un plan
  seulement » dans la synthèse.

Windows : `ai-hub-1.0.9.zip`, puis `installer.bat`.
Linux : `ai-hub-1.0.9-linux.tar.gz`, puis `sh installer.sh` et `./lancer.sh`.
Python 3.12 ou plus est nécessaire. Les espaces isolés nécessitent Docker Sandboxes.
Le schéma de la base ne change pas (26). Les empreintes des paquets sont dans `SHA256SUMS`.
