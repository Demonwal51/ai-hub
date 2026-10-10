"""End-to-end check of the access between conversations of a project, on the real CLI.

Two requests with the light model, in a throwaway database and folder:
1. "Couleurs" holds a decision (written straight into the database, no AI
   needed). "Accueil", in the same project, is asked what was decided: the
   real Claude Code must start the hub's MCP server, use its tools without
   any permission prompt, and find the answer.
2. "Privée", in the same project but not shared, is asked the same: it must
   get no access, so it cannot know.

    .venv\\Scripts\\python -B tools/essai_projets_reel.py
"""

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from aihub import db, store  # noqa: E402
from aihub.web.app import create_app  # noqa: E402

DECISION = "vert sapin"
results = []


def check(name, ok, detail=""):
    ok = bool(ok)
    results.append(ok)
    print(f"[{'OK  ' if ok else 'ÉCHEC'}] {name}" + (f" — {detail}" if detail else ""), flush=True)


def wait(client, cid, timeout=300):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        data = client.get(f"/api/conversations/{cid}").json()
        if data["pending"]:
            return data  # a permission prompt: the tools were not allowed on their own
        if not data["running"]:
            return data
        time.sleep(0.5)
    raise SystemExit(f"délai dépassé ({timeout} s)")


def main():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        work = tmp / "dossier"
        work.mkdir()
        db_path = str(tmp / "hub.db")
        app = create_app(db_path, permission_timeout_s=60)
        with TestClient(app) as client:
            pid = client.post("/api/projects", json={"name": "Boutique", "workdir": str(work)}).json()["project"]["id"]
            new = lambda title: client.post("/api/conversations", json={  # noqa: E731
                "title": title, "project_id": pid, "model": "haiku"}).json()["conversation"]["id"]
            colors = new("Couleurs")
            conn = db.connect(db_path)
            try:
                agent = store.get_conversation(conn, colors)["agent_id"]
                store.add_user_message(conn, colors, f"Pour le logo, on a choisi la couleur {DECISION}. C'est définitif.")
                mid = store.start_request(conn, colors, agent)
                store.mark_message_running(conn, mid)
                store.finish_message(conn, mid, "ok", content=f"Noté : le logo sera {DECISION}.")
            finally:
                conn.close()

            home = new("Accueil")
            client.post(f"/api/conversations/{home}/messages", json={"prompt": (
                "Quelle couleur a été choisie pour le logo dans la conversation « Couleurs » de ce projet ? "
                "Réponds en une phrase courte.")})
            data = wait(client, home)
            reply = data["messages"][-1]
            check("aucune demande d'autorisation pour les outils du hub", not data["pending"],
                  str([p["tool_name"] for p in data["pending"]]))
            check("réponse reçue", reply["status"] == "ok", f"{reply['status']} {reply['error']}")
            check("la décision de l'autre conversation est retrouvée", DECISION in (reply["content"] or "").lower(),
                  repr((reply["content"] or "")[:300]))
            print(f"   taille : {reply['tokens_in']} tokens envoyés, {reply['tokens_out']} reçus, "
                  f"{(reply['duration_ms'] or 0) / 1000:.1f} s")

            private = new("Privée")
            client.patch(f"/api/conversations/{private}", json={"shared": False})
            client.post(f"/api/conversations/{private}/messages", json={"prompt": (
                "Quelle couleur a été choisie pour le logo dans la conversation « Couleurs » de ce projet ? "
                "Si tu n'as aucun moyen de le savoir, réponds exactement : PAS D'ACCES.")})
            data = wait(client, private)
            reply = data["messages"][-1]
            check("conversation privée : réponse reçue", reply["status"] == "ok", f"{reply['status']} {reply['error']}")
            check("conversation privée : elle ne peut pas savoir", DECISION not in (reply["content"] or "").lower(),
                  repr((reply["content"] or "")[:300]))
    print(f"\n{sum(results)}/{len(results)} vérifications réussies")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
