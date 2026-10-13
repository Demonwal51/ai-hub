La mémoire RAG utilise les conversations et les documents ou notes explicitement associés au projet.

### Utilisation

- Dans une conversation : **⋯ → Mémoire RAG…**.
- Recherchez des passages, importez un document texte ou collez une note à
  associer au projet. Le propriétaire du projet gère ses sources.
- Formats : Markdown, TXT, CSV, JSON et YAML ; 50 documents par projet,
  200 000 caractères par document. Les lecteurs du projet peuvent consulter
  les sources, dans les limites des accès de la conversation et de l’espace.
- Les documents sont découpés, indexés et recherchés dans SQLite. Les passages
  sourcés sont transmis aux IA, au chef d’orchestre, aux sous-tâches et aux
  réponses comparées.
- La recherche par mots fonctionne sans Ollama ; la recherche par le sens
  s’ajoute lorsque son modèle d’embeddings est prêt. Aucun téléchargement automatique.
- Aucun coffre Obsidian n’est parcouru automatiquement. Les notes choisies
  peuvent être associées comme fichiers Markdown.
- Retirer une source l’exclut des recherches futures ; le fichier original et
  les réponses déjà produites restent inchangés.

### Installation

Windows : `ai-hub-1.0.6.zip`, puis `installer.bat`.
Linux : `ai-hub-1.0.6-linux.tar.gz`, puis `sh installer.sh` et `./lancer.sh`.
Python3.12 ou plus est nécessaire. Les espaces isolés nécessitent Docker Sandboxes.

La base passe au schéma25. Les sources RAG sont incluses dans les sauvegardes
SQLite ; conservez `data/` et une sauvegarde avant mise à jour.
Les empreintes des paquets sont dans `SHA256SUMS`.
