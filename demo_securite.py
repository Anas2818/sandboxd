"""Démo du bloc D (sécurité) et du bloc F (exploitation), étape par étape.

Prérequis : `make run` dans un autre terminal.
Lancer : python3 demo_securite.py   (Entrée pour passer à l'étape suivante)
"""
import json
import os
import time
import urllib.error
import urllib.request

API = os.environ.get("SANDBOXD_URL", "http://localhost:8000")
CLE = os.environ.get("SANDBOXD_API_KEY", "cle-operateur-demo")


def appel(methode, chemin, jeton=None, corps=None):
    """Envoie une requête, renvoie (code HTTP, réponse JSON ou texte)."""
    headers = {}
    if jeton:
        headers["Authorization"] = f"Bearer {jeton}"
    data = None
    if corps is not None:
        data = json.dumps(corps).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(API + chemin, data=data, method=methode, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=80) as r:
            code, brut = r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        code, brut = e.code, e.read().decode()
    try:
        return code, json.loads(brut)
    except ValueError:
        return code, brut


def etape(titre):
    input(f"\n=== {titre}  [Entrée]")


def montre(code, reponse, champs=None):
    if isinstance(reponse, dict) and champs:
        reponse = {k: reponse.get(k) for k in champs}
        for k in ("stderr", "stdout"):                     # on ne garde que la dernière ligne utile
            v = reponse.get(k)
            if isinstance(v, str) and v.strip():
                reponse[k] = v.strip().splitlines()[-1]
    print(f"-> {code}  {json.dumps(reponse, ensure_ascii=False) if not isinstance(reponse, str) else reponse}")


def creer():
    code, sb = appel("POST", "/v1/sandboxes", CLE, {"image": "python:3.12-slim"})
    for _ in range(60):
        code, sb = appel("GET", f"/v1/sandboxes/{sb['id']}", CLE)
        if sb["status"] != "pending":
            break
        time.sleep(1)
    return sb["id"], sb["status"]


def exec_agent(jeton, sid, cmd, timeout=10):
    return appel("POST", f"/v1/sandboxes/{sid}/exec", jeton, {"cmd": cmd, "timeout_seconds": timeout})


etape("1. Sans jeton : l'API refuse")
montre(*appel("GET", "/v1/sandboxes"), ["status", "detail"])

etape("2. Avec un faux jeton : refusé aussi")
montre(*appel("GET", "/v1/sandboxes", "je-suis-un-pirate"), ["status", "detail"])

etape("3. L'opérateur (clé d'API) crée deux sandboxes : A et B")
a, etat_a = creer()
b, etat_b = creer()
print(f"A = {a} ({etat_a})   B = {b} ({etat_b})")

etape("4. L'opérateur émet un jeton d'agent de 15 min pour A")
code, t = appel("POST", f"/v1/sandboxes/{a}/tokens", CLE)
jeton = t["token"]
print(f"-> {code}  expire à {t['expires_at']}")
print(f"   jeton : {jeton[:40]}…")

etape("5. L'agent exécute une commande dans SA sandbox (A)")
montre(*exec_agent(jeton, a, ["id"]), ["exit_code", "stdout"])

etape("6. L'agent essaie la sandbox d'un autre (B) : 404")
montre(*exec_agent(jeton, b, ["id"]), ["status", "detail"])

etape("7. L'agent essaie une action d'opérateur (détruire A) : 403")
montre(*appel("DELETE", f"/v1/sandboxes/{a}", jeton), ["status", "detail"])

etape("8. Attaque : accès réseau depuis la sandbox")
montre(*exec_agent(jeton, a, ["python", "-c",
       "import urllib.request; urllib.request.urlopen('http://example.com', timeout=5)"]),
       ["exit_code", "stderr"])

etape("9. Attaque : écrire hors de /workspace")
montre(*exec_agent(jeton, a, ["sh", "-c", "echo pirate > /etc/pirate"]), ["exit_code", "stderr"])
montre(*appel("GET", f"/v1/sandboxes/{a}/files?path=../../etc/passwd", jeton), ["status", "detail"])

etape("10. Attaque : fork bomb")
bombe = ("import os, time\n"
         "n = 0\n"
         "try:\n"
         "    while True:\n"
         "        if os.fork() == 0:\n"
         "            time.sleep(5); os._exit(0)\n"
         "        n += 1\n"
         "except OSError as e:\n"
         "    print('fork bomb stoppée après', n, 'processus :', e.strerror)\n")
montre(*exec_agent(jeton, a, ["python", "-c", bombe]), ["exit_code", "stdout"])

etape("11. Une commande trop longue est arrêtée par son timeout")
montre(*exec_agent(jeton, a, ["sleep", "30"], timeout=2), ["exit_code", "timed_out", "duration_ms"])

etape("12. Traçabilité : les 5 dernières lignes du journal d'audit")
try:
    with open("audit.log", encoding="utf-8") as f:
        for ligne in f.readlines()[-5:]:
            print("  ", ligne.rstrip())
except FileNotFoundError:
    print("   audit.log introuvable (lancer le script depuis le dossier du projet)")

etape("13. Exploitation : /metrics")
code, texte = appel("GET", "/metrics")
for ligne in texte.splitlines():
    if ligne.startswith("sandboxes_active") or ligne.startswith("sandboxd_request_errors_total") \
            or ('route="/v1/sandboxes/{sandbox_id}/exec"' in ligne and "requests_total" in ligne):
        print("  ", ligne)

etape("14. Ménage : l'opérateur détruit A et B")
print("A ->", appel("DELETE", f"/v1/sandboxes/{a}", CLE)[0], "   B ->", appel("DELETE", f"/v1/sandboxes/{b}", CLE)[0])
print("\nFin de la démo.")
