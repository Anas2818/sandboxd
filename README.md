# sandboxd

Anas Louraibi — Projet fil rouge, API REST pour l'administration système (4ESGI, 2026-27).

sandboxd est une API REST qui crée, pilote et détruit des **sandboxes** : des conteneurs Docker
isolés et jetables, dans lesquels on exécute du code sans risque. Deux clients :

- **l'opérateur** (un humain, une CI) : il crée, liste, prolonge et détruit les sandboxes ;
- **l'agent** : il exécute des commandes et lit ou écrit des fichiers, dans **sa** sandbox seulement.

Le contrat [`openapi.yaml`](openapi.yaml) fait foi : il sert de documentation (`make docs`),
de fausse API (`make mock`) et de test (`make test-contract`).

## Installer

Prérequis : Python 3.11+, Docker (et l'accès à son socket), Node 18+ pour `npx`.

```bash
make install                 # dépendances Python dans .venv
docker pull python:3.12-slim && docker pull node:22-slim && docker pull alpine:3.20
```

Sous Windows : travailler dans WSL, avec l'intégration WSL de Docker Desktop activée.

## Lancer

```bash
make run                     # sandboxd sur http://localhost:8000
curl localhost:8000/healthz
```

| Variable d'environnement | Rôle | Par défaut |
|---|---|---|
| `SANDBOXD_API_KEY` | Clé de l'opérateur | `cle-operateur-demo` (via le Makefile, **à changer** hors démo) |
| `SANDBOXD_JWT_SECRET` | Secret qui signe les jetons d'agent | tiré au hasard au démarrage |
| `SANDBOXD_MAX_SANDBOXES` | Quota de sandboxes actives | `20` |
| `SANDBOXD_AUDIT_LOG` | Fichier du journal d'audit | `audit.log` |
| `DOCKER_SOCKET` | Socket du démon Docker | `/var/run/docker.sock` |

## Commandes

| Commande | Rôle |
|---|---|
| `make lint` | Vérifie le contrat (Spectral et les règles du cours) |
| `make mock` | Fausse API à partir du contrat (Prism, port 4010) |
| `make docs` | Documentation HTML du contrat (`docs.html`) |
| `make run` | Lance sandboxd |
| `make test-contract` | Rejoue le contrat contre sandboxd (Schemathesis), avec la clé de l'opérateur |
| `make flaky` | API d'entraînement capricieuse du TP4 (port 8001) |
| `make clean` | Supprime les conteneurs (et leurs volumes) créés par sandboxd |
| `python3 demo_securite.py` | Démo pas à pas de la sécurité, de l'audit et des métriques |

## Exemples curl, une opération à la fois

Toutes les routes `/v1` demandent `Authorization: Bearer <jeton>` : la clé de l'opérateur,
ou un jeton d'agent. Les erreurs sont en `application/problem+json` (RFC 9457).

```bash
KEY=cle-operateur-demo
API=http://localhost:8000

# createSandbox : 202 + Location, la sandbox est en pending
curl -i -X POST $API/v1/sandboxes -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" -H "Idempotency-Key: $(uuidgen)" \
  -d '{"image": "python:3.12-slim", "ttl_seconds": 600}'

# listSandboxes : une page, et next_page_token pour la suivante
curl "$API/v1/sandboxes?page_size=20&status=ready" -H "Authorization: Bearer $KEY"

# getSandbox : l'état courant (pending, ready, failed)
curl $API/v1/sandboxes/sbx-a1b2c3d4 -H "Authorization: Bearer $KEY"

# updateSandbox : prolonger la durée de vie (JSON Merge Patch)
curl -X PATCH $API/v1/sandboxes/sbx-a1b2c3d4 -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/merge-patch+json" -d '{"ttl_seconds": 1800}'

# createAgentToken : un jeton d'agent de 15 min, limité à cette sandbox
curl -X POST $API/v1/sandboxes/sbx-a1b2c3d4/tokens -H "Authorization: Bearer $KEY"
TOKEN=...   # le champ "token" de la réponse

# writeFile : déposer un fichier dans /workspace (201 créé, 204 remplacé)
curl -X PUT "$API/v1/sandboxes/sbx-a1b2c3d4/files?path=hello.py" -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/octet-stream" --data-binary 'print("bonjour")'

# readFile : relire le fichier
curl "$API/v1/sandboxes/sbx-a1b2c3d4/files?path=hello.py" -H "Authorization: Bearer $TOKEN"

# execCommand : exécuter une commande (timeout de 1 à 60 s)
curl -X POST $API/v1/sandboxes/sbx-a1b2c3d4/exec -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" -d '{"cmd": ["python", "hello.py"], "timeout_seconds": 10}'

# deleteSandbox : 204, puis 404 au second appel (opérateur seulement)
curl -i -X DELETE $API/v1/sandboxes/sbx-a1b2c3d4 -H "Authorization: Bearer $KEY"

# Sondes (sans authentification)
curl $API/healthz        # le processus est vivant
curl $API/readyz         # Docker répond (sinon 503)
curl $API/metrics        # métriques RED et sandboxes_active
```

## Le client sbx

```bash
python client/sbx.py create --image python:3.12-slim --wait
python client/sbx.py ls
python client/sbx.py exec sbx-a1b2c3d4 -- python -c "print(1+1)"
python client/sbx.py rm sbx-a1b2c3d4
```

Il lit `SANDBOXD_URL` et `SANDBOXD_API_KEY`. Timeouts, nouveaux essais avec backoff,
`Retry-After`, `Idempotency-Key`, pagination jusqu'au bout : voir [`tp4.md`](tp4.md).

## Sécurité, en bref

| Mesure | Où |
|---|---|
| Clé d'opérateur lue dans l'environnement, comparée à temps constant | `app/main.py`, `qui()` |
| Jeton d'agent JWT HS256, 15 min, limité à une sandbox | `app/main.py`, `jeton_agent()`, `lire_jeton()` |
| 401 sans jeton valide, 403 pour un agent sur une action d'opérateur, 404 sur la sandbox d'un autre | `operateur()`, `acces_sandbox()` |
| Conteneur durci : pas de réseau, utilisateur 1000, `/` en lecture seule (sauf `/workspace` et `/tmp`), aucune capability, 64 processus, no-new-privileges | `app/docker.py`, `create_container()` |
| Quota de sandboxes actives (429), durée de vie appliquée (ramasseur toutes les 10 s), chemins sûrs (400) | `app/main.py` |
| Journal d'audit, une ligne JSON par action | `audit.log` |

Les failles étudiées sont décrites dans [`ATTAQUE.md`](ATTAQUE.md), l'exploitation dans [`RUNBOOK.md`](RUNBOOK.md).

## Livrables

| Fichier | Contenu |
|---|---|
| `openapi.yaml` | Le contrat, authentification comprise |
| `tp3.md` | Implémentation et tests de contrat (548 / 548) |
| `tp4.md` | Client robuste : ce qui échouait avant, ce qui tient maintenant |
| `ATTAQUE.md` | Rapport d'attaque, une fiche par faille |
| `RUNBOOK.md` | Trois incidents : symptômes, diagnostic, correction |

## Limites connues

- Le registre des sandboxes est **en mémoire** : un redémarrage le vide (les conteneurs restent,
  `make clean` les retrouve par leur étiquette `sandboxd=1`). Une base de données réglerait le problème.
- L'en-tête `Idempotency-Key` est validé, mais la déduplication côté serveur reste à faire.
- Pas encore fait : les webhooks (bloc E).
