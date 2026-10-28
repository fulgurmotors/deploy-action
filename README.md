# Déployer ton dépôt

Ce workflow réutilisable envoie le code de ton dépôt à deploy-hub et affiche le
journal dans GitHub Actions. Ton dépôt peut appartenir à ton compte personnel.
Aucun secret à ajouter, aucun jeton personnel ni clé de déploiement à maintenir.

## 1. Brancher le dépôt

Demande à un administrateur de brancher ton dépôt, sa branche et son dossier sur
une machine que tu as le droit de gérer. Le branchement peut aussi être fait dans
le panel quand cette fonction est disponible. Il doit précéder le premier push.

## 2. Ajouter le workflow

Créer `.github/workflows/deploy.yml` dans ton dépôt :

```yaml
name: Déployer
on:
  push:
    branches: [main]
  workflow_dispatch: {}
jobs:
  deploy:
    uses: fulgurmotors/deploy-action/.github/workflows/deploy.yml@v1
    permissions:
      id-token: write
      contents: read
```

Si tu utilises une autre branche, adapte à la fois le workflow et le branchement.
Les deux permissions sont nécessaires. Le workflow commun récupère exactement
le commit GitHub, sans conserver d'identifiants, puis produit un `git archive`.
Il n'exécute aucun script applicatif sur le runner : la construction reste dans
la machine cible.

## 3. Préparer le déploiement de l'application

Le fichier `deploy.sh`, à la racine, construit et redémarre ton application.
Pars du [modèle public](examples/deploy.sh)
(fourni aussi par l’administrateur ou le panel). Ce script s'exécute dans la machine cible avec les droits prévus
par le branchement ; vérifie son contenu avant de pousser.

Variables : OLD_SHA, NEW_SHA, BRANCH, REPO_DIR et CHANGED_FILES. Ce dernier est le
chemin d'un fichier listant les ajouts, modifications et suppressions. Pour décider
si les dépendances ont changé, lire ce fichier ; le dossier déployé n'est plus un
clone Git. Les données locales, `.env` et node_modules restent conservés, à
condition de ne pas les suivre dans Git. Une collision avec un fichier local est
refusée pour le préserver.

## Voir le résultat et résoudre un échec

Onglet **Actions**, exécution **Déployer**. Le journal s'actualise toutes les cinq
secondes ; l'exécution échoue si le déploiement échoue. Un push récent annule
l'attente de l'ancien job Actions ; le hub laisse finir le travail commencé et
ne conserve que le dernier travail en attente sur la même destination.

- « Ce dépôt n’est branché sur aucune machine » : faire vérifier le dépôt et la
  branche par un administrateur, puis relancer avec Run workflow ou un push.
- Permission `id-token: write` absente : recopier les deux permissions ci-dessus.
- Fichier modifié à la main ou collision : faire examiner la machine ; aucun
  écrasement automatique de ces modifications.
- `deploy.sh` en échec : lire ses lignes de journal. Une reprise au même commit
  peut nécessiter un `run --force` de l'administrateur après vérification.
- Transport ou suivi interrompu : vérifier d'abord le résultat dans le panel ou
  auprès de l'administrateur. Le client ne réexpédie jamais automatiquement le
  POST, dont l'issue pourrait déjà être un déploiement en cours.

Limites : 200 Mio compressés, 50 000 entrées et 2 Gio de données après extraction,
10 demandes par minute et par dépôt. Les sous-modules ne sont pas inclus. Le hub
conserve cinq archives pour un retour arrière par l'administrateur. Le suivi est
limité à trente minutes et à un tampon de 8 000 caractères ; une troncature entre
deux lectures est signalée. Ne pas afficher de secrets depuis deploy.sh.

## Maintenir le workflow

Les versions publiées sont `v1.0.0` et l'étiquette mobile `v1` (créées après revue).
Une évolution compatible déplace v1 ; une rupture publie v2. La signature OIDC
permet au hub de vérifier l'identité du dépôt, de sa branche et du workflow commun.
Les jetons de requête, OIDC et suivi restent en mémoire ; le POST transmet son
header via l'entrée standard de curl, pas via ses arguments ni un fichier.

`deploy.yml` embarque le client pour qu'un dépôt privé appelant n'ait à ajouter
que ce fichier et son deploy.sh. La source lisible est `scripts/deploy.py`.
Après modification, régénérer le workflow :

```sh
python3 scripts/render-workflow.py
python3 scripts/render-workflow.py --check
python3 -m unittest discover -s tests -v
```

Les tests utilisent des dépôts fictifs et un serveur HTTPS local avec certificat
temporaire. Ils couvrent l'archive, la limite de taille, les permissions OIDC,
l'échec et le succès, le suivi sans doublons, le masquage des jetons, l'absence de
réémission, l'isolation Python et un refus avant envoi d'une archive. La recette
d'un vrai dépôt privé suit l'installation du hub et de ses routes publiques.
