"""sandboxd — squelette du TP4.

Le contrat (openapi.yaml) fait foi : FastAPI ne génère pas le sien.
Fournis : le client Docker (docker.py), les erreurs problem+json (errors.py),
le stockage en mémoire, la tâche de création en arrière-plan et getSandbox
pour exemple. À vous d'écrire les autres routes : voir le sujet, partie 2.
"""
import json
import base64
import hashlib
import hmac
import os
import secrets
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, Query, Request, Response
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import docker
from .errors import Problem, install

app = FastAPI(title="sandboxd", openapi_url=None, docs_url=None, redoc_url=None)
install(app)

SANDBOXES: dict[str, dict] = {}  # id -> sandbox (en mémoire : perdue au redémarrage)
LOCK = threading.Lock()

# ---------- bloc D : réglages lus dans l'environnement ----------
API_KEY = os.environ.get("SANDBOXD_API_KEY", "")            # clé de l'opérateur
JWT_SECRET = os.environ.get("SANDBOXD_JWT_SECRET", "").encode() or secrets.token_bytes(32)
TOKEN_TTL = 15 * 60                                          # jeton d'agent : 15 minutes
MAX_SANDBOXES = int(os.environ.get("SANDBOXD_MAX_SANDBOXES", "20"))   # quota
AUDIT_FILE = os.environ.get("SANDBOXD_AUDIT_LOG", "audit.log")
AUDIT_LOCK = threading.Lock()


def now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(d: datetime) -> str:
    return d.isoformat().replace("+00:00", "Z")


def new_id() -> str:
    return "sbx-" + secrets.token_hex(4)


def public(sb: dict) -> dict:
    """La représentation JSON d'une sandbox, conforme au schéma Sandbox."""
    return {
        "id": sb["id"], "image": sb["image"], "status": sb["status"], "ttl_seconds": sb["ttl_seconds"],
        "limits": sb["limits"], "created_at": iso(sb["created_at"]),
        "expires_at": iso(sb["created_at"] + timedelta(seconds=sb["ttl_seconds"])),
    }


def get_or_404(sandbox_id: str) -> dict:
    sb = SANDBOXES.get(sandbox_id)
    if sb is None:
        raise Problem(404, "not-found", f"{sandbox_id} n'existe pas")
    return sb


async def read_json(request: Request, model: type[BaseModel], media_types: tuple[str, ...]) -> BaseModel:
    """Lit le corps : Content-Type refusé (415), JSON invalide (400), contenu invalide (422)."""
    ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if ctype not in media_types:
        raise Problem(415, "unsupported-media-type", f"attendu : {', '.join(media_types)}")
    try:
        data = json.loads(await request.body())
    except ValueError:
        raise Problem(400, "invalid-json", "le corps n'est pas du JSON valide")
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        first = exc.errors()[0]
        raise Problem(422, "validation", f"{'.'.join(map(str, first['loc'])) or 'corps'} : {first['msg']}")


def provision(sandbox_id: str) -> None:
    """Tâche de fond : crée le conteneur, puis passe la sandbox en ready ou failed.

    À lancer depuis createSandbox, après avoir répondu 202 :
        threading.Thread(target=provision, args=(sandbox_id,), daemon=True).start()
    """
    sb = SANDBOXES.get(sandbox_id)
    if sb is None:
        return
    try:
        cid = docker.create_container(sb["image"], sb["limits"]["cpus"], sb["limits"]["memory_mb"],
                                      {"sandboxd": "1", "sandboxd.id": sandbox_id})
        with LOCK:
            if sandbox_id not in SANDBOXES:  # détruite entre-temps
                docker.remove_container(cid)
                return
            sb.update(container_id=cid, status="ready")
    except Exception:  # Docker en erreur ou injoignable : la sandbox ne reste jamais bloquée en pending
        sb.update(status="failed")


# ---------- bloc D : authentification ----------
# Un seul en-tête, Authorization: Bearer <jeton>. Le jeton est soit la clé
# de l'opérateur, soit un jeton d'agent (JWT HS256) limité à une sandbox.

def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def signer(message: str) -> str:
    return b64url(hmac.new(JWT_SECRET, message.encode(), hashlib.sha256).digest())


def jeton_agent(sandbox_id: str, exp: int) -> str:
    """Fabrique un JWT : en-tête.contenu.signature (signature HMAC-SHA256)."""
    entete = b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    contenu = b64url(json.dumps({"sub": sandbox_id, "role": "agent", "iat": int(time.time()), "exp": exp}).encode())
    return f"{entete}.{contenu}.{signer(entete + '.' + contenu)}"


def lire_jeton(jeton: str) -> dict | None:
    """Renvoie le contenu du JWT s'il est authentique et pas expiré, sinon None."""
    try:
        entete, contenu, signature = jeton.split(".")
        if not hmac.compare_digest(signature, signer(entete + "." + contenu)):
            return None                                       # signature fausse : jeton fabriqué ou modifié
        if json.loads(b64url_decode(entete)).get("alg") != "HS256":
            return None
        claims = json.loads(b64url_decode(contenu))
        if claims.get("role") != "agent" or not isinstance(claims.get("sub"), str):
            return None
        if claims.get("exp", 0) < time.time():
            return None                                       # expiré
        return claims
    except (ValueError, AttributeError):
        return None


def non_authentifie(detail: str) -> Problem:
    return Problem(401, "unauthorized", detail, {"WWW-Authenticate": "Bearer"})


def qui(request: Request) -> dict:
    """Qui appelle ? 401 si personne de valide."""
    schema, _, jeton = request.headers.get("authorization", "").partition(" ")
    if schema.lower() != "bearer" or not jeton:
        raise non_authentifie("en-tête manquant : Authorization: Bearer <jeton>")
    if API_KEY and hmac.compare_digest(jeton.encode(), API_KEY.encode()):   # comparaison à temps constant
        acteur = {"role": "operator", "name": "operator"}
    else:
        claims = lire_jeton(jeton)
        if claims is None:
            raise non_authentifie("jeton invalide ou expiré")
        acteur = {"role": "agent", "sandbox": claims["sub"], "name": f"agent:{claims['sub']}"}
    request.state.acteur = acteur
    return acteur


def operateur(acteur: dict = Depends(qui)) -> dict:
    """Routes réservées à l'opérateur : un agent reçoit 403."""
    if acteur["role"] != "operator":
        raise Problem(403, "forbidden", "action réservée à l'opérateur")
    return acteur


def acces_sandbox(sandbox_id: str, acteur: dict = Depends(qui)) -> dict:
    """Un agent n'agit que sur SA sandbox : celle d'un autre renvoie 404 (on ne révèle pas qu'elle existe)."""
    if acteur["role"] == "agent" and acteur["sandbox"] != sandbox_id:
        raise Problem(404, "not-found", f"{sandbox_id} n'existe pas")
    return acteur


# ---------- bloc D : journal d'audit ; bloc F : logs JSON et métriques ----------

def audit(acteur: str, action: str, sandbox_id: str | None, status: int | None = None, request_id: str | None = None) -> None:
    """Une ligne JSON par action dans audit.log : qui, quoi, sur quelle sandbox, résultat."""
    ligne = json.dumps({"ts": iso(now()), "request_id": request_id, "actor": acteur, "action": action,
                        "sandbox_id": sandbox_id, "status": status}, ensure_ascii=False)
    with AUDIT_LOCK, open(AUDIT_FILE, "a", encoding="utf-8") as f:
        f.write(ligne + "\n")


METRICS_LOCK = threading.Lock()
REQUESTS: dict[tuple[str, str, int], int] = {}        # (méthode, route, statut) -> nombre
DURATIONS: dict[tuple[str, str], list[float]] = {}    # (méthode, route) -> [somme des durées, nombre]


@app.middleware("http")
async def suivi(request: Request, call_next):
    """Pour chaque requête : un request_id, une ligne de log JSON, les métriques, et l'audit des actions /v1."""
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
    debut = time.monotonic()
    response = await call_next(request)
    duree = time.monotonic() - debut
    route = getattr(request.scope.get("route"), "path", "autre")
    with METRICS_LOCK:
        cle = (request.method, route, response.status_code)
        REQUESTS[cle] = REQUESTS.get(cle, 0) + 1
        somme = DURATIONS.setdefault((request.method, route), [0.0, 0])
        somme[0] += duree
        somme[1] += 1
    acteur = getattr(request.state, "acteur", None)
    nom = acteur["name"] if acteur else "anonyme"
    response.headers["X-Request-ID"] = request_id
    print(json.dumps({"ts": iso(now()), "level": "info", "request_id": request_id, "method": request.method,
                      "path": request.url.path, "status": response.status_code,
                      "duration_ms": int(duree * 1000), "actor": nom}), flush=True)
    if request.url.path.startswith("/v1/"):
        audit(nom, f"{request.method} {route}", request.path_params.get("sandbox_id"), response.status_code, request_id)
    return response


@app.get("/metrics")
def metrics():
    """Métriques RED (Rate, Errors, Duration) et sandboxes_active, au format texte Prometheus."""
    lignes = ["# HELP sandboxd_requests_total Requêtes HTTP traitées.",
              "# TYPE sandboxd_requests_total counter"]
    with METRICS_LOCK:
        for (m, r, s), n in sorted(REQUESTS.items()):
            lignes.append(f'sandboxd_requests_total{{method="{m}",route="{r}",status="{s}"}} {n}')
        lignes += ["# HELP sandboxd_request_errors_total Requêtes terminées par une erreur serveur (5xx).",
                   "# TYPE sandboxd_request_errors_total counter",
                   f"sandboxd_request_errors_total {sum(n for (_, _, s), n in REQUESTS.items() if s >= 500)}",
                   "# HELP sandboxd_request_duration_seconds Durée des requêtes.",
                   "# TYPE sandboxd_request_duration_seconds summary"]
        for (m, r), (somme, nombre) in sorted(DURATIONS.items()):
            lignes.append(f'sandboxd_request_duration_seconds_sum{{method="{m}",route="{r}"}} {somme:.6f}')
            lignes.append(f'sandboxd_request_duration_seconds_count{{method="{m}",route="{r}"}} {nombre}')
    actives = sum(1 for sb in list(SANDBOXES.values()) if sb["status"] in ("pending", "ready"))
    lignes += ["# HELP sandboxes_active Sandboxes en pending ou ready.", "# TYPE sandboxes_active gauge",
               f"sandboxes_active {actives}"]
    return Response("\n".join(lignes) + "\n", media_type="text/plain; version=0.0.4")


@app.get("/readyz")
def readyz():
    """Prêt à servir ? Seulement si le démon Docker répond."""
    if not docker.ping():
        raise Problem(503, "docker-unavailable", "le démon Docker ne répond pas")
    return {"status": "ready"}


# ---------- bloc D : durée de vie appliquée ----------

def ramasseur() -> None:
    """Toutes les 10 s : détruit les sandboxes dont la durée de vie est dépassée."""
    while True:
        time.sleep(10)
        limite = now()
        with LOCK:
            expirees = [sb for sb in SANDBOXES.values()
                        if sb["created_at"] + timedelta(seconds=sb["ttl_seconds"]) <= limite]
            for sb in expirees:
                del SANDBOXES[sb["id"]]
        for sb in expirees:
            audit("system", "sandbox.expired", sb["id"])
            if sb["container_id"] is not None:
                try:
                    docker.remove_container(sb["container_id"])
                except Exception:
                    pass  # Docker injoignable : make clean les retrouvera par l'étiquette


threading.Thread(target=ramasseur, daemon=True).start()


# ---------- exemple fourni ----------

@app.get("/v1/sandboxes/{sandbox_id}")
def get_sandbox(sandbox_id: str, acteur: dict = Depends(acces_sandbox)):
    return public(get_or_404(sandbox_id))


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


# ---------- à vous ----------
# Une sandbox en mémoire est un dict :
#   {"id", "image", "status", "ttl_seconds", "limits": {"cpus", "memory_mb"},
#    "created_at", "container_id"}
class LimitsIn(BaseModel):                                   # la petite enveloppe
    model_config = ConfigDict(extra="forbid")
    cpus: float = Field(1, ge=0.1, le=2)
    memory_mb: int = Field(512, ge=64, le=1024)

# TODO createSandbox   POST   /v1/sandboxes                     -> 202 + Location
class SandboxCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    image: Literal["python:3.12-slim", "node:22-slim", "alpine:3.20"]
    ttl_seconds: int = Field(600, ge=60, le=3600)          
    limits: LimitsIn = Field(default_factory=LimitsIn)      # ← l'enveloppe rangée dans le formulaire

class SandboxPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ttl_seconds: int = Field(ge=60, le=3600)


@app.patch("/v1/sandboxes/{sandbox_id}")
async def update_sandbox(sandbox_id: str, request: Request, acteur: dict = Depends(operateur)):
    body = await read_json(request, SandboxPatch, ("application/merge-patch+json",))
    sb = get_or_404(sandbox_id)
    sb["ttl_seconds"] = body.ttl_seconds
    return public(sb)

@app.post("/v1/sandboxes")                                        
async def create_sandbox(request: Request, idempotency_key: str | None = Header(None, min_length=1, max_length=255),
                         acteur: dict = Depends(operateur)):
    body = await read_json(request, SandboxCreate, ("application/json",))
    sandbox_id = new_id()                          
    sb = {
        "id": sandbox_id,
        "image": body.image,
        "status": "pending",
        "ttl_seconds": body.ttl_seconds,
        "limits": body.limits.model_dump(),
        "created_at": now(),
        "container_id": None,
    }
    with LOCK:
        actives = sum(1 for s in SANDBOXES.values() if s["status"] in ("pending", "ready"))
        if actives >= MAX_SANDBOXES:   # quota atteint
            raise Problem(429, "quota-exceeded", f"{MAX_SANDBOXES} sandboxes actives au plus", {"Retry-After": "30"})
        SANDBOXES[sandbox_id] = sb
    threading.Thread(target=provision, args=(sandbox_id,), daemon=True).start()     
    return JSONResponse(                            
        public(sb),                                 
        status_code=202,
        headers={"Location": f"/v1/sandboxes/{sandbox_id}"},
    )                 

# TODO listSandboxes   GET    /v1/sandboxes

@app.get("/v1/sandboxes")
def list_sandboxes(
    status: Literal["pending", "ready", "failed", "terminated"] | None = None,
    page_size: int = Query(20, ge=1, le=100),
    page_token: str | None = Query(None, min_length=1),
    acteur: dict = Depends(operateur),
):
    toutes = sorted(SANDBOXES.values(), key=lambda sb: sb["id"])
    if status is not None:                                            
        toutes = [sb for sb in toutes if sb["status"] == status]      
    if page_token is not None:                                        
        try:                                                          
            apres = base64.urlsafe_b64decode(page_token).decode()    
        except ValueError:                                            
            raise Problem(400, "invalid-page-token", "marque-page inconnu")   
        if not apres.startswith("sbx-"):                              
            raise Problem(400, "invalid-page-token", "marque-page inconnu")   
        toutes = [sb for sb in toutes if sb["id"] > apres]            
    page = toutes[:page_size]
    if len(toutes) > page_size:
        derniere = page[-1]["id"]
        marque_page = base64.urlsafe_b64encode(derniere.encode()).decode()
    else:
        marque_page = None
    return {"items": [public(sb) for sb in page], "next_page_token": marque_page}

# TODO deleteSandbox   DELETE /v1/sandboxes/{sandbox_id}        -> 204
@app.delete("/v1/sandboxes/{sandbox_id}")
def delete_sandbox(sandbox_id: str, acteur: dict = Depends(operateur)):
    with LOCK:
        sb = get_or_404(sandbox_id)
        del SANDBOXES[sandbox_id]
    if sb["container_id"] is not None:
        docker.remove_container(sb["container_id"])
    return Response(status_code=204)

# TODO execCommand     POST   /v1/sandboxes/{sandbox_id}/exec   -> docker.exec_command(...)
class ExecRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cmd: list[Annotated[str, Field(min_length=1)]] = Field(min_length=1)
    timeout_seconds: int = Field(30, ge=1, le=60)


@app.post("/v1/sandboxes/{sandbox_id}/exec")
async def exec_command(sandbox_id: str, request: Request, acteur: dict = Depends(acces_sandbox)):
    body = await read_json(request, ExecRequest, ("application/json",))
    sb = get_or_404(sandbox_id)
    if sb["status"] != "ready":
        raise Problem(409, "not-ready", f"{sandbox_id} est en état {sb['status']}")
    return await run_in_threadpool(
        docker.exec_command, sb["container_id"], body.cmd, body.timeout_seconds
    )
    
# TODO readFile        GET    /v1/sandboxes/{sandbox_id}/files  -> docker.read_file(...)
MAX_FILE = 10 * 1024 * 1024


def chemin_sur(path: str) -> None:
    if path.startswith("/") or ".." in path.split("/"):
        raise Problem(400, "bad-path", f"{path} sort de /workspace")


def sandbox_prete(sandbox_id: str) -> dict:
    sb = get_or_404(sandbox_id)
    if sb["status"] != "ready":
        raise Problem(409, "not-ready", f"{sandbox_id} est en état {sb['status']}")
    return sb

@app.get("/v1/sandboxes/{sandbox_id}/files")
async def read_file(sandbox_id: str, path: str = Query(min_length=1, max_length=255),
                    acteur: dict = Depends(acces_sandbox)):
    sb = sandbox_prete(sandbox_id)
    chemin_sur(path)
    data = await run_in_threadpool(docker.read_file, sb["container_id"], path)
    if data is None:
        raise Problem(404, "file-not-found", f"{path} n'existe pas")
    return Response(content=data, media_type="application/octet-stream")

# TODO writeFile       PUT    /v1/sandboxes/{sandbox_id}/files  -> docker.write_file(...)
@app.put("/v1/sandboxes/{sandbox_id}/files")
async def write_file(sandbox_id: str, request: Request, path: str = Query(min_length=1, max_length=255),
                     acteur: dict = Depends(acces_sandbox)):
    sb = sandbox_prete(sandbox_id)
    chemin_sur(path)
    ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if ctype != "application/octet-stream":
        raise Problem(415, "unsupported-media-type", "attendu : application/octet-stream")
    data = await request.body()
    if len(data) > MAX_FILE:
        raise Problem(413, "payload-too-large", "10 Mo au plus")
    cree = await run_in_threadpool(docker.write_file, sb["container_id"], path, data)
    return Response(status_code=201 if cree else 204)
    
# Bonus updateSandbox  PATCH  /v1/sandboxes/{sandbox_id}        -> ETag et If-Match


# ---------- bloc D : jeton d'agent ----------
@app.post("/v1/sandboxes/{sandbox_id}/tokens")
def create_token(sandbox_id: str, acteur: dict = Depends(operateur)):
    """L'opérateur obtient un jeton d'agent de 15 min, valable pour CETTE sandbox seulement."""
    get_or_404(sandbox_id)
    exp = int(time.time()) + TOKEN_TTL
    return JSONResponse(
        {"token": jeton_agent(sandbox_id, exp), "token_type": "Bearer",
         "sandbox_id": sandbox_id, "expires_at": iso(datetime.fromtimestamp(exp, timezone.utc))},
        status_code=201,
    )
