# RUNBOOK — sandboxd

Trois incidents, chacun avec ses symptômes, son diagnostic et sa correction.

## 1. Une sandbox reste bloquée en pending

**Symptômes**
- `GET /v1/sandboxes/<id>` renvoie `"status": "pending"` depuis plus d'une minute.
- `sbx create --wait` abandonne au bout de 60 s ; `exec` répond **409 not-ready**.

**Diagnostic**
1. Le démon Docker répond-il ? `curl localhost:8000/readyz` (503 = non, voir incident 2).
2. L'image est-elle en cours de téléchargement ? La première création d'une image peut être longue :
   `docker images | grep -E "python|node|alpine"`.
3. Le conteneur existe-t-il ? `docker ps -a --filter label=sandboxd.id=<id>`.
4. Les logs de sandboxd (une ligne JSON par requête, avec `request_id`).

**Correction**
- Image manquante : `docker pull python:3.12-slim` (idem pour les deux autres), puis recréer la sandbox.
- Docker injoignable : voir incident 2. Depuis le correctif du TP4, la création échoue proprement et la
  sandbox passe en `failed` au lieu de rester en `pending`.
- Sandbox inutilisable : la détruire (`sbx rm <id>`) et en recréer une. Les sandboxes oubliées sont de
  toute façon détruites par le ramasseur à l'expiration de leur `ttl_seconds`.

## 2. Docker indisponible

**Symptômes**
- `curl localhost:8000/readyz` → **503** `docker-unavailable` (alors que `/healthz` répond 200).
- Les nouvelles sandboxes passent en `failed` ; `docker ps` échoue.
- Sous WSL : « The command 'docker' could not be found in this WSL 2 distro ».

**Diagnostic**
1. `docker ps` : le démon répond-il ?
2. Le socket existe-t-il ? `ls -l /var/run/docker.sock` (ou la valeur de `DOCKER_SOCKET`).
3. Sous Windows : Docker Desktop est-il démarré, avec l'intégration WSL activée pour la distribution ?

**Correction**
- Démarrer Docker (Docker Desktop, ou `sudo systemctl start docker` sous Linux).
- Sous WSL : Docker Desktop → Settings → Resources → WSL integration → activer la distribution.
- Mauvais socket : `make run DOCKER_SOCKET=<chemin>`.
- Vérifier : `/readyz` repasse à 200. Recréer les sandboxes restées en `failed`.
- Après un redémarrage de sandboxd, le registre en mémoire est vide mais les conteneurs restent :
  `make clean` les supprime grâce à l'étiquette `sandboxd=1`.

## 3. Rotation de la clé opérateur

**Quand** : la clé a fuité (dans un dépôt Git, un log, une capture d'écran), un membre de l'équipe part,
ou simplement à intervalle régulier.

**Symptômes d'une fuite**
- Dans `audit.log`, des actions `operator` que personne n'a faites, ou des créations en série
  (le quota renvoie alors des **429**).

**Procédure**
1. Générer une nouvelle clé : `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`.
2. La mettre dans l'environnement du serveur et le redémarrer :
   `make run SANDBOXD_API_KEY=<nouvelle clé>`.
3. La donner aux clients légitimes (CI, `sbx`) : `export SANDBOXD_API_KEY=<nouvelle clé>`.
4. Vérifier : l'ancienne clé reçoit **401**, la nouvelle fonctionne
   (`curl -i localhost:8000/v1/sandboxes -H "Authorization: Bearer <clé>"`).
5. Relire `audit.log` pour mesurer ce que l'ancienne clé a pu faire.

**À savoir**
- Le redémarrage vide le registre en mémoire : prévenir les utilisateurs et faire `make clean`.
- Si `SANDBOXD_JWT_SECRET` n'est pas fixé, il change à chaque démarrage : tous les jetons d'agent
  deviennent invalides (401) et doivent être réémis. S'il a fuité lui aussi, le changer de la même façon.
