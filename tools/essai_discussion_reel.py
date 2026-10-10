"""End-to-end check of the discussion with the conductor, on the real Claude Code CLI.

Three requests with the light model: the conductor proposes a plan, the
user asks it to drop a part (discussion, which resumes the planning
session), then "Adapter le plan" must apply what was said. Nothing is run.

    python -B tools/essai_discussion_reel.py
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


def wait(client, cid, timeout=300):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        data = client.get(f"/api/conversations/{cid}").json()
        if not data["running"]:
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
            cid = client.post("/api/conversations", json={
                "title": "essai discussion", "workdir": str(work), "mode": "orchestration",
                "conductor_model": "haiku"}).json()["conversation"]["id"]
            client.post(f"/api/conversations/{cid}/messages", json={"prompt": (
                "Crée trois fichiers : rouge.txt contenant « rouge », vert.txt contenant « vert », "
                "bleu.txt contenant « bleu ».")})
            data = wait(client, cid)
            plan = data["plans"][0] if data["plans"] else None
            check("plan proposé", plan, data["messages"][-1]["content"][:150])
            if not plan:
                return 1
            titles = [s["title"] for s in plan["subtasks"]]
            print(f"   sous-tâches : {titles}")

            client.post(f"/api/conversations/{cid}/messages", json={
                "prompt": "Finalement je ne veux pas de bleu.txt. Que changerais-tu au plan ?",
                "kind": "discuss"})
            data = wait(client, cid)
            reply = data["messages"][-1]
            check("le chef répond en discussion (session reprise)", reply["status"] == "ok" and reply["content"],
                  f"{reply['status']} {reply['error']} : {reply['content'][:300]!r}")
            check("la discussion n'a rien écrit", not any(work.iterdir()))
            check("le plan n'a pas changé tout seul", len(data["plans"][0]["subtasks"]) == len(titles))

            client.post("/api/agents", json={"model": "haiku"})
            client.post(f"/api/plans/{plan['id']}/adapt")
            data = wait(client, cid)
            plan = data["plans"][0]
            # Judged on the declared files: a description may rightly say
            # "do not create bleu.txt".
            files = sorted({f.lower() for s in plan["subtasks"] for f in s["files"]})
            print(f"   après adaptation : {[s['title'] for s in plan['subtasks']]}, fichiers {files}")
            for st in plan["subtasks"]:
                print(f"     {st['position']}. {st['description'][:400]}")
            check("l'adaptation applique la discussion (bleu.txt n'est plus prévu)",
                  not any("bleu" in f for f in files) and any("rouge" in f for f in files)
                  and any("vert" in f for f in files), files)
            check("chaque sous-tâche a un agent", all(s["agent_id"] for s in plan["subtasks"]))
    print(f"\n{sum(results)}/{len(results)} vérifications réussies")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
