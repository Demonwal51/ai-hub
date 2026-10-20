AI Hub 1.3.0 trouve tout seul Claude Code et Codex sur le PC.

- **Fini « [WinError 2] »** : Claude Code et Codex sont cherchés à chaque lancement ; une mise
  à jour de l’application Codex qui déplace sa CLI ne casse plus rien.
- **Une recherche plus large** : PATH, applications Claude et Codex, installeur officiel, npm,
  winget, extensions d’éditeur, et sous Linux `~/.local/bin`, nvm, `/usr/local/bin`, Homebrew.
- **« Chercher sur ce PC »** (Réglages › IA proposées et sauvegardes) : les disques locaux, une
  minute au plus ; **Retenir** garde la copie choisie dans les réglages.
- Un message clair quand rien n’est trouvé, et le diagnostic dit si Claude et Codex sont là.
- Avec la 1.2.4 : la mise à jour en un clic vérifiée par son empreinte SHA256.

Windows : `ai-hub-1.3.0.zip`, puis `installer.bat`.
Linux : `ai-hub-1.3.0-linux.tar.gz`, puis `sh installer.sh` et `./lancer.sh`.
Python 3.12 ou plus est nécessaire. Les espaces isolés nécessitent Docker Sandboxes.
La base reste au schéma 29 (depuis la 1.2.1) ; conservez `data/` et une sauvegarde avant la mise à jour.
Les empreintes des paquets sont dans `SHA256SUMS`.
