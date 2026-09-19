# Heroes — Téléchargements Windows

Le client Heroes réunit BrawlHeroes, MoveHeroes et RogueHeroes. [Télécharge le client sur le site Heroes](https://heroes-website-beta.vercel.app/telecharger) après connexion avec Discord. Extrais toute l’archive, ouvre `HeroesClient/HeroesClient.exe`, connecte-toi puis sélectionne un jeu et clique sur **Installer**.

Les jeux sont des prototypes en développement. Le client propose leurs mises à jour à la demande. Les jeux installés restent accessibles hors connexion.

Les archives des jeux et leurs empreintes SHA-256 restent disponibles dans les releases. Conserve les DLL et dossiers voisins de chaque exécutable. Le moteur Unity n’a pas besoin d’être installé sur ton PC.

Le client est distribué depuis un stockage privé, avec un lien temporaire fourni par le site. Les anciennes releases du client sont conservées en brouillon pour les mainteneurs ; leurs liens publics ne sont plus disponibles.

Ce dépôt contient uniquement les informations de distribution et les jeux compilés. Les projets de développement sont conservés séparément. Le fichier `catalog.json` permet au client de retrouver les versions proposées.

Pour les mainteneurs : ne pas republier les archives du client dans les releases publiques. Les futures versions doivent passer par le stockage privé et le parcours Discord du site.

## Catalogue automatique des jeux

Chaque dépôt de jeu déclenche **Synchronize Heroes catalog** après publication d’une release stable. L’automatisation sélectionne la version stable la plus élevée, vérifie le manifeste de tests et le commit du tag, télécharge le ZIP et son SHA-256, contrôle le contenu Unity et sa provenance, puis publie une copie des deux fichiers dans ce dépôt. Un téléchargement public complet est vérifié avant la modification de `catalog.json`.

Les brouillons et préversions sont exclus. Les archives publiées ne sont jamais écrasées. Une version ancienne ne peut pas faire reculer le catalogue ; les mises à jour simultanées de plusieurs jeux fusionnent leurs entrées avec vérification du SHA du fichier. En cas de panne, relancer le workflow : il reprend les pièces jointes déjà vérifiées. Un contrôle quotidien rattrape un événement manqué, notamment une publication sur un ancien tag qui ne contient pas encore le workflow.

Le lecteur du dépôt source est le `GITHUB_TOKEN` temporaire du jeu, limité à la lecture. **Heroes Catalog Publisher**, application GitHub privée installée uniquement sur `Heroes-Releases`, fournit un jeton temporaire d’écriture pour la distribution ; il est révoqué à la fin du job. La clé privée est chiffrée dans les secrets Actions des trois jeux. Aucun jeton personnel permanent n’est utilisé par cette automatisation. Les actions et le code de publication sont fixés à des commits vérifiés.

Les scripts publics sous `tools/` contiennent seulement l’automatisation et ses tests, sans code des jeux. Les anciennes URL `Nayir/Heroes-Releases` sont conservées dans le catalogue pour les clients déjà distribués ; ne pas recréer ce dépôt, car cela casserait les redirections.

Validation locale : `python -m unittest discover -s tools -v`.
