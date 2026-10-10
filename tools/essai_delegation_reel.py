"""End-to-end check of the delegation on the real CLIs: Claude (Haiku) asks Codex (GPT-6 Luna).

Costs a little of both quotas.

    .venv\\Scripts\\python -B tools/essai_delegation_reel.py
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aihub import config, db, routing, store  # noqa: E402
from aihub.runner import Request  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"[{'OK  ' if ok else 'ÉCHEC'}] {name}" + (f" — {detail}" if detail else ""), flush=True)


def main():
    routing.set_available({"claude_code": None, "codex": None})  # both connected, as the server finds them
    settings = config.validate({"summary_model": "aucun", "models": ["haiku", "gpt-6-luna"],
                                "default_model": "haiku", "default_conductor": "haiku"})
    with tempfile.TemporaryDirectory() as folder:
        conn = db.connect(str(Path(folder) / "hub.db"))
        try:
            agent = store.ensure_agent(conn, "claude_code", "haiku")
            cid = store.create_conversation(conn, "Délégation", agent_id=agent, workdir=folder)
            store.set_tools(conn, cid, ["deleguer"])
            result = Request(conn, cid, agent, "Avec l'outil ask_ai (serveur deleguer), demande au modèle gpt-6-luna "
                             "combien font 17 fois 23, puis donne sa réponse en une phrase.", settings=settings,
                             timeout_s=600).run()
            rows = [dict(r) for r in conn.execute("SELECT * FROM delegations")]
        finally:
            conn.close()
    check("réponse de Claude", result.status == "ok", result.error or "")
    check("l'outil de délégation appelé", "mcp__deleguer__ask_ai" in result.tool_calls, str(result.tool_calls))
    check("avis enregistré, de GPT-6 Luna", rows and rows[0]["model"] == "gpt-6-luna" and rows[0]["status"] == "ok",
          repr([(r["model"], r["status"], (r["answer"] or r["error"] or "")[:80]) for r in rows]))
    check("la bonne réponse", "391" in (result.text or ""), repr((result.text or "")[:200]))
    print(f"   Claude : {result.tokens_in} tokens envoyés, {result.num_turns} appel(s), {result.duration_ms / 1000:.1f} s")
    if rows:
        print(f"   GPT-6 Luna : {rows[0]['tokens_in']} tokens envoyés, {rows[0]['duration_ms'] / 1000:.1f} s")
    print(f"\n{sum(results)}/{len(results)} vérifications réussies")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
