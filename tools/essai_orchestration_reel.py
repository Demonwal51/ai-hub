"""End-to-end check of orchestration against the real Claude Code CLI.

The conductor (light model) splits "create two independent files", two
agents (light model) create them, permissions are approved through the API,
and the conductor writes the synthesis. Throwaway folder and database.

    python -B tools/essai_orchestration_reel.py
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


def wait(client, cid, predicate, timeout=300):
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
            r = client.post("/api/conversations", json={
                "title": "essai orchestration", "workdir": str(work), "mode": "orchestration",
                "conductor_model": "haiku"})
            cid = r.json()["conversation"]["id"]
            client.post(f"/api/conversations/{cid}/messages", json={"prompt": (
                "Crée deux fichiers dans le dossier de travail : un.txt contenant exactement le texte « un », "
                "et deux.txt contenant exactement le texte « deux ». Ce sont deux tâches indépendantes.")})
            data = wait(client, cid, lambda d: not d["running"])
            reply = data["messages"][-1]
            plans = data["plans"]
            check("le chef propose un plan", plans and reply["status"] == "ok",
                  f"{reply['status']} {reply['content'][:150]!r} {reply['error']}")
            if not plans:
                return 1
            plan = plans[0]
            print(f"   résumé : {plan['summary']}")
            print(f"   agents en parallèle choisis par le chef : {plan['max_parallel']}")
            for st in plan["subtasks"]:
                print(f"   {st['position']}. {st['title']} — fichiers {st['files']} — dépend de {st['depends_on']}")
            check("rien n'a été écrit pendant la planification", not any(work.iterdir()),
                  sorted(p.name for p in work.iterdir()))
            check("les fichiers de chaque sous-tâche sont déclarés",
                  all(st["files"] for st in plan["subtasks"]))

            agent = client.post("/api/agents", json={"model": "haiku"}).json()["agent"]["id"]
            client.post(f"/api/plans/{plan['id']}/assign-all", json={"agent_id": agent})
            r = client.post(f"/api/plans/{plan['id']}/launch")
            check("plan lancé", r.status_code == 202, r.text[:200])
            asked = []
            data = wait(client, cid, lambda d: d["pending"] or not d["running"])
            for _ in range(20):
                if not data["running"]:
                    break
                for p in data["pending"]:
                    asked.append((p["subtask_title"], p["tool_name"]))
                    client.post(f"/api/permissions/{p['id']}", json={"allow": True})
                data = wait(client, cid, lambda d: d["pending"] or not d["running"])
            plan = data["plans"][0]
            print(f"   autorisations demandées : {asked}")
            check("toutes les sous-tâches réussies", plan["status"] == "done",
                  [(s["title"], s["status"], s["error"]) for s in plan["subtasks"]])
            contents = {name: (work / name).read_text(encoding="utf-8-sig").strip()
                        if (work / name).exists() else None for name in ("un.txt", "deux.txt")}
            check("les deux fichiers sont écrits", contents == {"un.txt": "un", "deux.txt": "deux"}, contents)
            synthesis = next((m for m in data["messages"] if m["id"] == plan["synthesis_message_id"]), None)
            check("synthèse du chef", synthesis and synthesis["status"] == "ok",
                  synthesis and synthesis["content"][:300])
            started = [s["started_at"] for s in plan["subtasks"]]
            print(f"   départs des sous-tâches : {started}")
    print(f"\n{sum(results)}/{len(results)} vérifications réussies")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
