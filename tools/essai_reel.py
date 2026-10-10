"""End-to-end check against the real Claude Code CLI, signed in.

Uses a throwaway working folder and database, and the light model, to spend
as little quota as possible. Six requests: simple answer, resumed session,
allowed write, denied write, plan mode, stop during a command.

    python -B tools/essai_reel.py [--model haiku]
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aihub import db, store  # noqa: E402
from aihub.providers.claude_code import Decision  # noqa: E402
from aihub.runner import Request  # noqa: E402

results = []


def check(name, ok, detail=""):
    ok = bool(ok)
    results.append(ok)
    print(f"[{'OK  ' if ok else 'ÉCHEC'}] {name}" + (f" — {detail}" if detail else ""), flush=True)


def sleeping_processes():
    """Processes still running a 60-second pause, other than this check itself."""
    if os.name != "nt":
        return []
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "Get-CimInstance Win32_Process | Select-Object ProcessId, Name, CommandLine | ConvertTo-Json"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=0x08000000,
    )
    try:
        rows = json.loads(proc.stdout or "[]")
    except ValueError:
        return [f"liste illisible : {proc.stdout[:200]}"]
    pattern = re.compile(r"(?i)sleep\b.*\b60\b")
    return [(r["ProcessId"], r["Name"]) for r in rows
            if r.get("CommandLine") and pattern.search(r["CommandLine"])]


def conversation(conn, agent, workdir, mode="manual", title="essai"):
    return store.create_conversation(conn, title, agent_id=agent, workdir=str(workdir),
                                     permission_mode=mode, confirm_bypass=(mode == "bypass"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="haiku")
    args = parser.parse_args()

    if os.name == "nt":
        # The leftover-process check must see a live pause, or its "nothing
        # left" verdict at the end would prove nothing.
        probe = subprocess.Popen(["powershell", "-NoProfile", "-Command", "Start-Sleep -Seconds 60"],
                                 creationflags=0x08000000)
        try:
            seen = any(pid == probe.pid for pid, _ in sleeping_processes())
        finally:
            probe.kill()
            probe.wait()
        check("la vérification voit une pause vivante", seen)
        check("la vérification voit qu'elle a disparu",
              all(pid != probe.pid for pid, _ in sleeping_processes()))

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        conn = db.connect(str(tmp / "hub.db"))
        try:
            agent = store.create_agent(conn, "Claude Code", "claude_code", args.model)

            # 1. Simple answer, streamed.
            work = tmp / "simple"
            work.mkdir()
            cid = conversation(conn, agent, work)
            chunks = []
            r = Request(conn, cid, agent, "Réponds uniquement par le mot : bonjour",
                        on_text=chunks.append, timeout_s=180).run()
            check("réponse simple", r.status == "ok" and "bonjour" in r.text.lower(),
                  f"statut {r.status}, texte {r.text!r}, erreur {r.error}")
            check("texte reçu en flux", len(chunks) >= 1, f"{len(chunks)} morceau(x)")
            check("mesures", r.duration_ms > 0 and (r.tokens_in or 0) > 0 and (r.tokens_out or 0) > 0,
                  f"{r.duration_ms} ms, {r.tokens_in} tokens envoyés, {r.tokens_out} reçus, modèle {r.model}")
            quota = store.latest_quota(conn, "claude_code")
            check("quota relevé", quota is not None,
                  dict(quota) if quota else "aucun rate_limit_event reçu")

            # 2. The next message resumes the session.
            r = Request(conn, cid, agent, "Quel mot viens-tu de répondre ? Réponds par ce mot seul.",
                        timeout_s=180).run()
            check("reprise de la session", r.status == "ok" and "bonjour" in r.text.lower(),
                  f"texte {r.text!r}")

            # 3. Manual mode, write allowed.
            work = tmp / "autorise"
            work.mkdir()
            cid = conversation(conn, agent, work)
            asked = []

            def allow(req):
                asked.append(req.tool_name)
                return Decision(True)

            r = Request(conn, cid, agent,
                        "Crée le fichier note.txt contenant exactement le texte : essai aihub",
                        ask_permission=allow, timeout_s=180).run()
            note = work / "note.txt"
            # The model picks the tool (Write, PowerShell...); PowerShell may add a BOM.
            content = note.read_text(encoding="utf-8-sig").strip() if note.exists() else None
            check("écriture autorisée", r.status == "ok" and content == "essai aihub" and asked,
                  f"outils demandés {asked}, contenu {content!r}, erreur {r.error}")
            mid = store.list_messages(conn, cid)[-1]["id"]
            journal = [(p["tool_name"], p["decision"]) for p in store.list_permissions(conn, message_id=mid)]
            check("autorisations journalisées", journal == [(t, "allow") for t in asked],
                  f"journal {journal}, demandés {asked}")

            # 4. Manual mode, write denied.
            work = tmp / "refuse"
            work.mkdir()
            cid = conversation(conn, agent, work)
            asked = []

            def deny(req):
                asked.append(req.tool_name)
                return Decision(False, "refusé pour l'essai")

            r = Request(conn, cid, agent,
                        "Crée le fichier note.txt contenant exactement le texte : essai aihub",
                        ask_permission=deny, timeout_s=180).run()
            check("écriture refusée", not (work / "note.txt").exists() and len(asked) >= 1,
                  f"statut {r.status}, outils demandés {asked}")

            # 5. Plan mode: nothing changes until the plan is approved.
            work = tmp / "plan"
            work.mkdir()
            cid = conversation(conn, agent, work, mode="plan")
            asked = []
            r = Request(conn, cid, agent, "Crée le fichier plan.txt contenant : test",
                        ask_permission=deny, timeout_s=180).run()
            check("mode plan sans modification", not (work / "plan.txt").exists(),
                  f"statut {r.status}, outils demandés {asked}")

            # 6. Stop while a command runs. Claude Code does not ask before a
            # harmless command such as a pause, so the stop is armed at launch
            # rather than on a permission prompt.
            work = tmp / "arret"
            work.mkdir()
            cid = conversation(conn, agent, work)
            tools = []

            def allow(req):
                tools.append(req.tool_name)
                return Decision(True)

            request = Request(
                conn, cid, agent,
                "Lance la commande shell qui attend 60 secondes (Start-Sleep -Seconds 60 sous "
                "PowerShell), puis réponds : fini.",
                ask_permission=allow, timeout_s=120)
            # 1.5 s after launch the request is still running whatever the
            # model does with the pause: the one-word answer above takes
            # about 3 s from start to finish.
            timer = threading.Timer(1.5, request.stop)
            timer.start()
            try:
                r = request.run()
            finally:
                timer.cancel()
            reply = store.list_messages(conn, cid)[-1]
            check("arrêt pendant une requête", r.status == "stopped" and r.duration_ms < 30000,
                  f"statut {r.status} après {r.duration_ms} ms, outils demandés {tools}, "
                  f"texte reçu avant l'arrêt {reply['content'][:200]!r}")
            leftovers = sleeping_processes()
            check("commande de l'agent tuée", not leftovers, f"encore vivants : {leftovers}")
            check("conversation libérée après l'arrêt", store.active_request(conn, cid) is None)

            quota = store.latest_quota(conn, "claude_code")
            print("\nDernier quota relevé :", dict(quota) if quota else None)
        finally:
            conn.close()

    print(f"\n{sum(results)}/{len(results)} vérifications réussies")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
