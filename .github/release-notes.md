Cette version corrige la reprise après l’annulation d’un plan d’orchestration
et après le refus d’un plan proposé par Claude.

### Nouveautés

- **Annuler le plan** libère immédiatement la saisie, même si l’actualisation
  suivante échoue. Les autres appareils ouverts reçoivent le changement.
- **Refuser** un plan termine la requête qui attendait sa validation et permet
  de reformuler immédiatement une nouvelle demande dans la conversation.
- Le plan n’est pas autorisé, et les permissions choisies restent inchangées.
- Les demandes d’autorisation encore en mémoire de cette requête arrêtée
  sont ignorées pour ne pas créer une nouvelle attente.
- La confirmation d’annulation distingue **Annuler le plan** et
  **Conserver le plan**.

### Installation

- Windows : extrayez `ai-hub-1.0.3.zip`, puis lancez `installer.bat`.
- Linux : extrayez `ai-hub-1.0.3-linux.tar.gz`, puis lancez `sh installer.sh`
  et `./lancer.sh`.
- Python3.12 ou plus est nécessaire. Les espaces isolés nécessitent Docker
  Sandboxes et une connexion à votre fournisseur IA dans chaque espace.

Conservez votre dossier `data/` et une sauvegarde de la base avant mise à jour.
Le schéma reste en version24. Le moteur Docker Sandboxes natif Linux avec KVM
reste à valider séparément ; cela ne change pas la compatibilité Linux de l’application.

Les fichiers `SHA256SUMS` permettent de vérifier les empreintes des paquets.
