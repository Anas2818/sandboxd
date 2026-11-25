"""sbx — le client en ligne de commande de sandboxd. TP4 : client robuste.

    python -m client.sbx ls [--status ready]
    python -m client.sbx create --image alpine:3.20 [--wait]
    python -m client.sbx exec sbx-a1b2c3d4 -- ls -la
    python -m client.sbx rm sbx-a1b2c3d4

L'adresse de l'API vient de SANDBOXD_URL (par défaut http://localhost:8000).
"""
import argparse
import os
import random
import sys
import time
import uuid

import httpx

API = os.environ.get("SANDBOXD_URL", "http://localhost:8000")
API_KEY = os.environ.get("SANDBOXD_API_KEY", "cle-operateur-demo")  # bloc D : clé de l'opérateur

TIMEOUT = httpx.Timeout(5.0)              # aucun appel ne reste bloqué plus de 5 s
MAX_ESSAIS = 6                            # au-delà, on abandonne
ATTENTE_MAX = 60                          # --wait abandonne après 60 s
REFUS_AVANT_TRAITEMENT = {429, 503}       # le serveur n'a rien fait : on peut toujours réessayer
REPONSE_INCERTAINE = {502, 504}           # le serveur a peut-être agi : seulement si c'est sans risque


class ApiError(Exception):
    """Erreur définitive : inutile de réessayer."""


def attendre(retry_after: str | None, essai: int) -> None:
    """Respecte Retry-After s'il est donné, sinon un backoff exponentiel avec du hasard."""
    if retry_after is not None:
        try:
            delai = float(retry_after)
        except ValueError:
            delai = 1.0
    else:
        delai = min(8.0, 0.5 * 2 ** (essai - 1)) * random.uniform(0.5, 1.5)
    time.sleep(delai)


def request(client: httpx.Client, method: str, path: str, *, timeout=TIMEOUT, **kwargs) -> httpx.Response:
    """Envoie une requête et renvoie la réponse finale, en réessayant ce qui peut l'être."""
    headers = kwargs.get("headers") or {}
    sans_risque = method in ("GET", "PUT", "DELETE") or "Idempotency-Key" in headers
    for essai in range(1, MAX_ESSAIS + 1):
        dernier = essai == MAX_ESSAIS
        try:
            r = client.request(method, path, timeout=timeout, **kwargs)
        except httpx.TransportError as exc:  # pas de réponse : réseau coupé ou timeout
            if sans_risque and not dernier:
                attendre(None, essai)
                continue
            raise ApiError(f"{method} {path} : pas de réponse ({exc.__class__.__name__})")
        if r.status_code in REFUS_AVANT_TRAITEMENT and not dernier:
            attendre(r.headers.get("Retry-After"), essai)
            continue
        if r.status_code in REPONSE_INCERTAINE and sans_risque and not dernier:
            attendre(r.headers.get("Retry-After"), essai)
            continue
        return r
    return r


def check(r: httpx.Response) -> httpx.Response:
    """Transforme une réponse d'erreur en ApiError, avec le message du serveur."""
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail", r.text)
        except ValueError:
            detail = r.text
        raise ApiError(f"{r.request.method} {r.request.url.path} : {r.status_code} {detail}")
    return r


def cmd_ls(client: httpx.Client, args) -> None:
    """Suit next_page_token jusqu'au bout, une ligne par sandbox."""
    params = {"page_size": 100}
    if args.status:
        params["status"] = args.status
    while True:
        page = check(request(client, "GET", "/v1/sandboxes", params=params)).json()
        for sb in page["items"]:
            print(f'{sb["id"]}\t{sb["status"]}\t{sb["image"]}')
        if not page["next_page_token"]:
            break
        params["page_token"] = page["next_page_token"]


def cmd_create(client: httpx.Client, args) -> None:
    """Création rejouable (une Idempotency-Key réutilisée à chaque essai), puis --wait."""
    cle = str(uuid.uuid4())
    r = check(request(client, "POST", "/v1/sandboxes", json={"image": args.image},
                      headers={"Idempotency-Key": cle}))
    sb = r.json()
    if args.wait:
        adresse = r.headers.get("Location") or f'/v1/sandboxes/{sb["id"]}'
        limite = time.monotonic() + ATTENTE_MAX
        while sb["status"] == "pending":
            if time.monotonic() > limite:
                raise ApiError(f'{sb["id"]} toujours en pending après {ATTENTE_MAX} s')
            time.sleep(1)
            sb = check(request(client, "GET", adresse)).json()
        if sb["status"] != "ready":
            raise ApiError(f'{sb["id"]} est en état {sb["status"]}')
    print(sb["id"])


def cmd_exec(client: httpx.Client, args) -> int:
    """Affiche stdout et stderr séparément, et sort avec le code de la commande."""
    r = check(request(client, "POST", f"/v1/sandboxes/{args.id}/exec", json={"cmd": args.cmd},
                      timeout=httpx.Timeout(5.0, read=70.0)))
    resultat = r.json()
    sys.stdout.write(resultat["stdout"])
    sys.stderr.write(resultat["stderr"])
    if resultat["timed_out"]:
        print("sbx: commande arrêtée, timeout dépassé", file=sys.stderr)
        return 124
    return resultat["exit_code"]


def cmd_rm(client: httpx.Client, args) -> None:
    """Un 404 n'est pas une erreur : DELETE est idempotent, la sandbox n'existe plus."""
    r = request(client, "DELETE", f"/v1/sandboxes/{args.id}")
    if r.status_code == 404:
        return
    check(r)


def main() -> int:
    parser = argparse.ArgumentParser(prog="sbx")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("ls"); p.add_argument("--status"); p.set_defaults(func=cmd_ls)
    p = sub.add_parser("create"); p.add_argument("--image", required=True); p.add_argument("--wait", action="store_true"); p.set_defaults(func=cmd_create)
    p = sub.add_parser("exec"); p.add_argument("id"); p.add_argument("cmd", nargs=argparse.REMAINDER); p.set_defaults(func=cmd_exec)
    p = sub.add_parser("rm"); p.add_argument("id"); p.set_defaults(func=cmd_rm)
    args = parser.parse_args()
    if getattr(args, "cmd", None) and args.cmd[:1] == ["--"]:
        args.cmd = args.cmd[1:]
    with httpx.Client(base_url=API, headers={"Authorization": f"Bearer {API_KEY}"}) as client:  # un seul client : connexions réutilisées
        try:
            return args.func(client, args) or 0
        except ApiError as exc:
            print(f"sbx: {exc}", file=sys.stderr)
            return 1


if __name__ == "__main__":
    sys.exit(main())
