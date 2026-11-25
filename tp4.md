# TP4 — Client robuste

Anas Louraibi — API REST pour l'administration système, 2026-27

Le client `sbx` (`client/sbx.py`) doit fonctionner même quand l'API répond mal. Les mesures ont été faites
contre l'API capricieuse du kit, lancée avec `FLAKY_SEED=42 make flaky` pour rejouer le même scénario.

## Le constat de départ (client naïf du squelette)

| Mesure | Résultat | Attendu |
|---|---|---|
| `sbx ls \| wc -l` | **20** sandboxes | 500 |
| 10 × `sbx create` | **7** réussites, **3** plantages (`KeyError: 'id'`) | 10 réussites |
| Sandboxes réellement créées (`/_stats`) | **8** | 10 |

`/_stats` après ce constat : 11 requêtes, 1 × 429, 1 × 503, 1 réponse lente, 1 réponse perdue, 8 créations.

## Ce qui échouait, et pourquoi

| Problème | Cause |
|---|---|
| `ls` ne voit que 20 sandboxes sur 500 | Le client lit la première page et ignore `next_page_token`. |
| `create` plante sur un 429 ou un 503 | Le client ne regarde pas le code de réponse : il cherche un `id` dans une réponse d'erreur. Il ne réessaie jamais, et n'attend pas `Retry-After`. |
| Une sandbox « fantôme » | Sur un 502 (réponse perdue), la sandbox est créée mais le client plante sans connaître son numéro : 8 créées pour 7 numéros affichés. Un simple nouvel essai aurait créé un doublon. |
| Une réponse lente peut bloquer le client | Aucun timeout n'est fixé sur les appels. |

## Ce que fait le client maintenant

Toutes les commandes passent par `request()` :

- **Timeout** de 5 s sur chaque appel (70 s en lecture pour `exec`, qui peut durer jusqu'à 60 s).
- **Nouveaux essais** seulement pour 429, 502, 503, 504 et les erreurs réseau ; jamais pour 400, 404 ou 422.
  - 429 et 503 : le serveur a refusé avant de traiter, on peut toujours réessayer.
  - 502, 504 et erreurs réseau : le serveur a peut-être agi. On ne réessaie que si c'est sans risque :
    GET, PUT, DELETE, ou une création qui porte une `Idempotency-Key`. Un POST sans clé ne se rejoue pas.
- **Attente** : `Retry-After` s'il est donné, sinon un backoff exponentiel (0,5 s, 1 s, 2 s, 4 s, 8 s au plus)
  multiplié par un facteur au hasard entre 0,5 et 1,5 (jitter).
- **Abandon** après 6 essais, avec un message clair et un code de sortie 1.

Et dans les commandes :

- `sbx ls` suit `next_page_token` jusqu'à `null`.
- `sbx create` envoie une `Idempotency-Key` (un UUID), réutilisée à chaque nouvel essai ; `--wait` interroge
  l'adresse donnée par `Location` jusqu'à `ready` ou `failed`.
- `sbx exec` affiche `stdout` et `stderr` séparément et sort avec le code de la commande (124 si timeout).
- `sbx rm` accepte un 404 : DELETE est idempotent.
- Un seul `httpx.Client` par commande : les connexions sont réutilisées.

## Les mêmes mesures avec le nouveau client

| Vérification | Résultat |
|---|---|
| `sbx ls \| wc -l` | **500** |
| `sbx ls \| sort \| uniq -d` | aucun doublon |
| 20 × `sbx create` | **20** réussites, aucune erreur |
| `sbx create --wait` | le numéro s'affiche une fois la sandbox `ready` |
| `sbx rm` deux fois de suite | code de sortie 0 les deux fois |

`/_stats` après ces tests :

```json
{"requests":61,"fail_429":5,"fail_503":5,"slow":8,"lost":2,"created":21,"idempotent_replays":2,"sandboxes":521}
```

- 10 refus (429 et 503) et 8 réponses lentes, tous absorbés par les nouveaux essais.
- 2 réponses perdues, et 2 rejeux idempotents : les deux créations perdues ont été retrouvées grâce à
  l'`Idempotency-Key`, sans doublon.
- 21 créations = 20 `create` + 1 `create --wait` : exactement le nombre demandé.

## Contre ma sandboxd

Avec `SANDBOXD_URL=http://localhost:8000` : `create --wait`, `exec`, `ls` et `rm` fonctionnent de la même façon.
