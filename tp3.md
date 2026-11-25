# TP3 — Implémentation de sandboxd

Anas Louraibi — API REST pour l'administration système, 2026-27

L'API est codée dans `app/main.py` (Python, FastAPI) et respecte le contrat `openapi.yaml`.
Toutes les opérations du contrat sont implémentées :

| Opération | Méthode et chemin | Réponse |
|---|---|---|
| createSandbox | `POST /v1/sandboxes` | 202 + `Location`, sandbox en `pending`, conteneur créé en tâche de fond (`provision`) |
| listSandboxes | `GET /v1/sandboxes` | 200, filtre `status`, pagination par curseur (`page_size`, `page_token`, `next_page_token`) |
| getSandbox | `GET /v1/sandboxes/{sandbox_id}` | 200 (fourni par le squelette) |
| updateSandbox | `PATCH /v1/sandboxes/{sandbox_id}` | 200, JSON Merge Patch, modifie `ttl_seconds` |
| deleteSandbox | `DELETE /v1/sandboxes/{sandbox_id}` | 204, puis 404 au second appel |
| execCommand | `POST /v1/sandboxes/{sandbox_id}/exec` | 200 + ExecResult ; 409 si la sandbox n'est pas `ready` |
| readFile | `GET /v1/sandboxes/{sandbox_id}/files?path=…` | 200, contenu brut ; 404 si absent |
| writeFile | `PUT /v1/sandboxes/{sandbox_id}/files?path=…` | 201 si créé, 204 si remplacé |

Les chemins de fichiers qui sortent de `/workspace` (chemin absolu ou contenant `..`) sont refusés en 400
avant tout appel à Docker, car `app/docker.py` ne les vérifie pas.

## Dernière sortie de `make test-contract`

```
.venv/bin/schemathesis run openapi.yaml --url http://localhost:8000 -n 20 -w 1 \
	--exclude-checks positive_data_acceptance
Schemathesis v4.34.2
━━━━━━━━━━━━━━━━━━━━

 ✅  Loaded specification from openapi.yaml (in 4.37s)

     Base URL:         http://localhost:8000
     Specification:    Open API 3.1.0
     Operations:       9 selected / 9 total

 ✅  API capabilities:

     Supports NULL byte in headers:                            ✘
     Accepts backslash and control characters in URL paths:    ✓
     Cache:                                                    1 stale removed

 ✅  Examples (in 1.35s)

     ✅ 7 passed  ⏭  2 skipped

 ✅  Coverage (in 6.51s)

     ✅ 9 passed

 ✅  Fuzzing (in 12.39s)

     ✅ 9 passed

 ✅  Stateful (in 6.24s)

     Scenarios:    21
     API Links:    18 covered / 22 selected / 22 total (22 inferred)

     ✅ 21 passed

=================================== SUMMARY ====================================

API Operations:
  Selected: 9/9
  Tested: 9

Test Phases:
  ✅ Examples
  ✅ Coverage
  ✅ Fuzzing
  ✅ Stateful

Test cases:
  548 generated, 548 passed

Seed: 175221875657671359944203554177165180271

========================== No issues found in 27.38s ===========================
```

## Écarts code / contrat corrigés

Le premier passage de `make test-contract` a généré 487 cas et trouvé 6 échecs, qui correspondent à 4 écarts.
Pour chacun, j'ai choisi de corriger soit le code, soit le contrat.

| # | Écart détecté | Contrôle | Décision | Correction |
|---|---|---|---|---|
| 1 | `GET` et `PUT /files` sans paramètre `path` renvoient 422, code non documenté | ST103 | **Contrat** | Ajout de la réponse `422` (`UnprocessableContent`) à `readFile` et `writeFile` |
| 2 | `exec` avec `{"cmd": [""]}` est accepté alors que chaque argument doit faire au moins 1 caractère | ST201 | **Code** | `cmd: list[Annotated[str, Field(min_length=1)]] = Field(min_length=1)` |
| 3 | Un en-tête `Idempotency-Key` de plus de 255 caractères est accepté | ST201 | **Code** | L'en-tête est lu et validé : `Header(None, min_length=1, max_length=255)` |
| 4 | `PATCH /v1/sandboxes/{sandbox_id}` renvoie 405, et l'en-tête `Allow` ne liste pas PATCH | ST103, ST205 | **Code** | Implémentation de `updateSandbox` (modèle `SandboxPatch`, `application/merge-patch+json`) |

Justification des choix :

1. **Contrat** : un `path` manquant est une requête bien formée mais incomplète. Le 422 renvoyé par FastAPI est
   un refus légitime et cohérent avec `listSandboxes`, qui documente déjà 422 pour ses paramètres. Le contrat
   devait simplement l'annoncer.
2. **Code** : le contrat a raison. Un argument vide n'a pas de sens, et la commande échouait dans le conteneur
   (`exit_code` 127) au lieu d'être refusée par l'API.
3. **Code** : le contrat fixe une longueur maximale, mais le code ignorait l'en-tête. Il est maintenant validé.
   La mémorisation des clés pour rendre la création rejouable (bonus) n'est pas encore faite.
4. **Code** : le contrat promet la prolongation d'une sandbox. Il fallait écrire la route manquante.

Après ces corrections, le second passage génère 548 cas et n'en fait échouer aucun.

## Limite connue

Les sandboxes sont stockées en mémoire, comme dans le squelette. À chaque redémarrage du serveur, le registre
est vidé, mais les conteneurs restent dans Docker. `make clean` les supprime grâce à l'étiquette `sandboxd=1`.
