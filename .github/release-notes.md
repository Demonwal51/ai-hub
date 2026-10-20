AI Hub 1.3.1 corrige les plans proposés par Codex.

- **Codex peut de nouveau proposer un plan** : OpenAI refusait la forme imposée au plan
  (« Invalid schema for response_format … additionalProperties … strict »).
- Avec la 1.3.0 : AI Hub trouve tout seul Claude Code et Codex sur le PC, et « Chercher sur ce
  PC » (Réglages › IA proposées et sauvegardes) retient la copie choisie.

Windows : `ai-hub-1.3.1.zip`, puis `installer.bat`.
Linux : `ai-hub-1.3.1-linux.tar.gz`, puis `sh installer.sh` et `./lancer.sh`.
Python 3.12 ou plus est nécessaire. Les espaces isolés nécessitent Docker Sandboxes.
La base reste au schéma 29 (depuis la 1.2.1) ; conservez `data/` et une sauvegarde avant la mise à jour.
Les empreintes des paquets sont dans `SHA256SUMS`.
