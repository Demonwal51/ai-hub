"""End-to-end check of the web server against the real Claude Code CLI.

Two requests with the light model, through the HTTP API exactly as the
interface sends them: a one-word answer, then a file write approved from the
API. Throwaway folder and database.

    python -B tools/essai_web_reel.py
"""

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from aihub.web.app import create_app  # noqa: E402

results = []


def check(name, ok, detail=""):
    ok = bool(ok)
    results.append(ok)
    print(f"[{'OK  ' if ok else 'ÉCHEC'}] {name}" + (f" — {detail}" if detail else ""), flush=True)


def wait(client, cid, predicate, timeout=180):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        data = client.get(f"/api/conversations/{cid}").json()
        if predicate(data):
            return data
        time.sleep(0.5)
    raise SystemExit(f"délai dépassé ({timeout} s)")


def main():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        work = tmp / "dossier"
        work.mkdir()
        app = create_app(str(tmp / "hub.db"))
        with TestClient(app) as client:
            status = client.get("/api/status").json()
            check("Claude Code trouvé et connecté", status["logged_in"], status)
            r = client.post("/api/conversations", json={"title": "essai web", "workdir": str(work),
                                                       "model": "haiku"})
            cid = r.json()["conversation"]["id"]

            client.post(f"/api/conversations/{cid}/messages",
                        json={"prompt": "Réponds uniquement par le mot : bonjour"})
            data = wait(client, cid, lambda d: not d["running"])
            reply = data["messages"][-1]
            check("réponse via le serveur", reply["status"] == "ok" and "bonjour" in reply["content"].lower(),
                  f"{reply['status']} {reply['content']!r} {reply['error']}")

            client.post(f"/api/conversations/{cid}/messages",
                        json={"prompt": "Crée le fichier note.txt contenant exactement le texte : essai web"})
            data = wait(client, cid, lambda d: d["pending"] or not d["running"])
            asked = [p["tool_name"] for p in data["pending"]]
            check("demande d'autorisation reçue par le serveur", asked, f"outils {asked}")
            for _ in range(5):  # the model may ask for several tools in a row
                if not data["pending"]:
                    break
                for p in data["pending"]:
                    client.post(f"/api/permissions/{p['id']}", json={"allow": True})
                data = wait(client, cid, lambda d: d["pending"] or not d["running"])
            note = work / "note.txt"
            content = note.read_text(encoding="utf-8-sig").strip() if note.exists() else None
            check("fichier écrit après accord", content == "essai web", f"contenu {content!r}")

            quota = client.get("/api/quota").json()["claude_code"]
            check("quota exposé par l'API", quota and quota["windows"], quota and quota["windows"])
    print(f"\n{sum(results)}/{len(results)} vérifications réussies")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
