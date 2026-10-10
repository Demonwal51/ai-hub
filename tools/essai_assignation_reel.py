"""End-to-end check of the automatic assignment on the real Claude Code CLI.

One request (light model): the conductor plans a job mixing kinds of work;
the plan shows how it classified each part and which AI the benchmark
table chose. Nothing is run; the plan is cancelled.

    python -B tools/essai_assignation_reel.py
"""

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from aihub.web.app import create_app  # noqa: E402

REQUEST = (
    "Trois choses indépendantes : 1) écris un script PowerShell sauvegarde.ps1 qui copie le dossier "
    "documents vers une clé USB ; 2) écris un README.md qui explique à un débutant comment s'en servir ; "
    "3) vérifie que le script ne peut pas effacer de fichiers par erreur (audit de sécurité, rapport audit.md)."
)
EXPECTED = {"terminal", "redaction", "securite"}
results = []


def check(name, ok, detail=""):
    ok = bool(ok)
    results.append(ok)
    print(f"[{'OK  ' if ok else 'ÉCHEC'}] {name}" + (f" — {detail}" if detail else ""), flush=True)


def main():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        work = tmp / "dossier"
        work.mkdir()
        app = create_app(str(tmp / "hub.db"))
        with TestClient(app) as client:
            cid = client.post("/api/conversations", json={
                "title": "essai assignation", "workdir": str(work), "mode": "orchestration",
                "conductor_model": "haiku", "assignment_mode": "auto"}).json()["conversation"]["id"]
            client.post(f"/api/conversations/{cid}/messages", json={"prompt": REQUEST})
            deadline = time.monotonic() + 300
            while client.get(f"/api/conversations/{cid}").json()["running"] and time.monotonic() < deadline:
                time.sleep(0.5)
            data = client.get(f"/api/conversations/{cid}").json()
            check("plan proposé", data["plans"], data["messages"][-1]["content"][:150])
            if not data["plans"]:
                return 1
            plan = data["plans"][0]
            kinds = set()
            for st in plan["subtasks"]:
                kinds.add(st["kind"])
                print(f"   {st['position']}. {st['title']}")
                print(f"      genre : {st['kind']} → {st['model']}")
                print(f"      raison : {st['assignment_reason']}")
            check("chaque sous-tâche a une IA et une raison",
                  all(s["agent_id"] and s["assignment_reason"] for s in plan["subtasks"]))
            check("le chef a vu les trois genres de travail", EXPECTED <= kinds, sorted(kinds))
            client.post(f"/api/plans/{plan['id']}/cancel")
    print(f"\n{sum(results)}/{len(results)} vérifications réussies")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
