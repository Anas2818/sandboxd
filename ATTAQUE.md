# Rapport d'attaque

Anas Louraibi — sandboxd, bloc D (sécurité).

Le principe : le code qui tourne dans une sandbox est écrit par un agent, donc **non fiable**.
Chaque fiche décrit une faille, comment la reproduire, son impact, le correctif et le test qui le prouve.
Les tests se rejouent avec `python3 demo_securite.py` (numéro d'étape indiqué).

## 1. API ouverte à tous

- **Reproduction** : avant le bloc D, `curl -X DELETE localhost:8000/v1/sandboxes/<id>` sans rien d'autre.
- **Impact** : n'importe qui sur le réseau crée, lit, exécute et détruit des sandboxes.
- **Correctif** : chaque route `/v1` exige `Authorization: Bearer <jeton>`. L'opérateur a une clé lue dans
  `SANDBOXD_API_KEY` (jamais dans le code), comparée avec `hmac.compare_digest` pour ne pas laisser
  deviner la clé caractère par caractère au temps de réponse.
- **Test** : sans jeton → **401** ; avec un faux jeton → **401** (étapes 1 et 2).

## 2. Un agent agit sur la sandbox d'un autre

- **Reproduction** : un agent qui connaît (ou devine) l'identifiant d'une autre sandbox appelle son `exec`.
- **Impact** : lecture des fichiers et du code d'un autre client, exécution chez lui.
- **Correctif** : l'agent reçoit un **jeton JWT** émis par `POST /v1/sandboxes/{id}/tokens`, signé en
  HMAC-SHA256, valable 15 min, qui contient l'identifiant de **sa** sandbox. Sur une autre sandbox, l'API
  répond **404** et non 403, pour ne pas révéler qu'elle existe. Modifier le jeton casse la signature.
- **Test** : jeton de A sur A → 200 ; sur B → **404** ; pour détruire A → **403** (étapes 5, 6 et 7).

## 3. Accès réseau depuis la sandbox

- **Reproduction** : `exec` de `python -c "import urllib.request; urllib.request.urlopen('http://example.com')"`.
- **Impact** : télécharger un outil d'attaque, envoyer des données volées, attaquer d'autres machines.
- **Correctif** : `NetworkMode: none` à la création du conteneur (`app/docker.py`).
- **Test** : l'appel échoue (pas de résolution de nom), code de sortie non nul (étape 8).

## 4. Écriture hors de /workspace et droits root

- **Reproduction** : `exec` de `sh -c "echo pirate > /etc/pirate"`, ou `readFile` avec `path=../../etc/passwd`.
- **Impact** : modifier le système du conteneur, lire des fichiers sensibles, préparer une évasion.
- **Correctif** :
  - utilisateur **1000** (non root) dans le conteneur ;
  - `ReadonlyRootfs` : tout est en lecture seule sauf `/workspace` (volume) et `/tmp` (tmpfs, `noexec`) ;
  - `CapDrop: ALL` et `no-new-privileges` : aucun privilège noyau, pas de retour à root par setuid ;
  - côté API, `chemin_sur()` refuse les chemins absolus et ceux qui contiennent `..` (**400**).
- **Test** : `id` affiche `uid=1000` (étape 5) ; l'écriture dans `/etc` échoue avec « Read-only file system » ;
  `../../etc/passwd` → **400** (étape 9).

## 5. Fork bomb

- **Reproduction** : un programme qui se duplique sans fin (`os.fork()` en boucle).
- **Impact** : la table des processus de la machine se remplit, tout le serveur se bloque, pas seulement la sandbox.
- **Correctif** : `PidsLimit: 64` sur le conteneur.
- **Test** : la boucle est stoppée par « Resource temporarily unavailable » après une soixantaine de processus (étape 10).

## 6. Épuisement des ressources

- **Reproduction** : créer des sandboxes en boucle, ou ne jamais les détruire.
- **Impact** : mémoire et CPU de la machine épuisés ; les autres clients ne peuvent plus travailler.
- **Correctif** :
  - limites CPU et mémoire par sandbox (liste blanche de bornes dans le contrat), sans swap en plus ;
  - **quota** de 20 sandboxes actives (`SANDBOXD_MAX_SANDBOXES`) : au-delà, **429** avec `Retry-After` ;
  - **durée de vie appliquée** : un ramasseur détruit toutes les 10 s les sandboxes expirées ;
  - `timeout -s KILL` sur chaque commande exécutée.
- **Test** : une commande de 30 s avec `timeout_seconds: 2` est arrêtée (`timed_out: true`, étape 11).

## 7. Aucune trace de qui a fait quoi

- **Impact** : après un incident, impossible de savoir quel client a agi.
- **Correctif** : `audit.log`, une ligne JSON par action `/v1` : date, `request_id`, acteur
  (`operator` ou `agent:sbx-…`), action, sandbox, code de réponse. Les refus (401, 403, 404) y figurent aussi.
- **Test** : étape 12 de la démo.

## Faille connue, non corrigée : le lien symbolique

Un agent peut créer dans `/workspace` un lien vers un autre dossier (`ln -s /etc /workspace/etc`), puis
demander `readFile` sur `etc/passwd` : le chemin ne contient pas `..`, il passe le contrôle de l'API.
Correctif prévu : résoudre le chemin réel dans le conteneur et refuser tout ce qui ne commence pas par
`/workspace/`. C'est un bonus du sujet.
