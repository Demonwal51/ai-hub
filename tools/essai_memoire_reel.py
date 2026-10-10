"""End-to-end check of the memory (RAG) on the real Claude Code CLI, with Haiku.

A conversation holds a decision buried among other messages, and its session
is one the CLI does not have (as after a change of PC or account). The next
question must: fail to resume, get a summary written by Haiku, start a new
session fed by the memory, and answer with the decision.

    .venv\\Scripts\\python -B tools/essai_memoire_reel.py
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aihub import db, memory, store  # noqa: E402
from aihub.runner import Request  # noqa: E402

DECISION = "vert sapin"
LOST = "00000000-0000-4000-8000-000000000000"
results = []


def check(name, ok, detail=""):
    ok = bool(ok)
    results.append(ok)
    print(f"[{'OK  ' if ok else 'ÉCHEC'}] {name}" + (f" — {detail}" if detail else ""), flush=True)


def main():
    with tempfile.TemporaryDirectory() as tmp:
        conn = db.connect(str(Path(tmp) / "hub.db"))
        try:
            agent = store.ensure_agent(conn, "claude_code", "haiku")
            cid = store.create_conversation(conn, "Boutique", agent_id=agent, workdir=tmp)
            said = [("On refait le site de la boutique, en trois pages.", "D'accord : accueil, catalogue, contact."),
                    ("La police sera Inter partout.", "Noté, Inter."),
                    (f"Pour le logo, on a choisi la couleur {DECISION}. C'est définitif.", f"Noté : logo {DECISION}."),
                    ("Les marges : 24 px sur ordinateur, 16 px sur téléphone.", "Noté pour les marges.")]
            for request, reply in said:
                store.add_user_message(conn, cid, request)
                mid = store.start_request(conn, cid, agent)
                store.mark_message_running(conn, mid)
                store.finish_message(conn, mid, "ok", content=reply, session_id=LOST)
            check("la mémoire contient la conversation",
                  conn.execute("SELECT COUNT(*) FROM memory WHERE conversation_id = ?", (cid,)).fetchone()[0] == 8)
            result = Request(conn, cid, agent, "Rappelle-moi la couleur du logo, en une phrase courte.").run()
            summary = memory.get_summary(conn, cid)
        finally:
            conn.close()
    check("réponse reçue", result.status == "ok", f"{result.status} {result.error or ''}")
    check("nouveau départ automatique (session introuvable)", result.fresh_start == "automatic", result.fresh_start)
    check("résumé écrit par Haiku", summary and DECISION in summary["content"].lower(),
          repr((summary or {}).get("content", "")[:300]))
    if summary:
        print(f"   résumé : {summary['tokens_in']} tokens envoyés, {summary['tokens_out']} reçus")
    check("la réponse donne la décision", DECISION in (result.text or "").lower(), repr((result.text or "")[:200]))
    print(f"   requête : {result.tokens_in} tokens envoyés, {result.tokens_out} reçus, "
          f"{result.num_turns} appel(s), outils {result.tool_calls}, {result.duration_ms / 1000:.1f} s")
    print(f"\n{sum(results)}/{len(results)} vérifications réussies")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
