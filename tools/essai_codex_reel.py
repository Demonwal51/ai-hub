"""End-to-end check of the Codex provider on the real Codex CLI, with GPT-6 Luna.

Costs a little of the ChatGPT quota (Luna, the lightest model; one short
request to GPT-6 Astra when offered). Checks:
the free readings (login, models, quota), a reply, a write asked and
allowed, a write refused, the session resumed, a question about another
conversation of a project (the hub's read-only MCP server), and a
conversation that goes from Claude (Haiku) to Codex with the memory.

    .venv\\Scripts\\python -B tools/essai_codex_reel.py [--sans-claude]
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aihub import config, db, store  # noqa: E402
from aihub.providers import codex  # noqa: E402
from aihub.providers.claude_code import Decision  # noqa: E402
from aihub.runner import Request  # noqa: E402

MODEL = "gpt-6-luna"  # the lightest (codex-cli 0.162); gpt-5.6-luna before
results = []


def check(name, ok, detail=""):
    ok = bool(ok)
    results.append(ok)
    print(f"[{'OK  ' if ok else 'ÉCHEC'}] {name}" + (f" — {detail}" if detail else ""), flush=True)


def measures(result):
    return (f"{result.tokens_in} tokens envoyés, {result.tokens_out} reçus, {result.num_turns} appel(s), "
            f"outils {result.tool_calls}, {result.duration_ms / 1000:.1f} s")


def run(cli, folder, prompt, **kwargs):
    kwargs.setdefault("permission_mode", "manual")
    return codex.CodexRun(cli, prompt=prompt, workdir=folder, model=MODEL, timeout_s=300, **kwargs).run()


def free_readings(cli, folder):
    check("Codex connecté", codex.login_status(cli))
    models = codex.list_models(cli, workdir=folder)
    check(f"{MODEL} proposé", MODEL in models, ", ".join(models))
    quota = codex.read_usage(cli, workdir=folder)
    check("quota lu sans rien dépenser", quota.windows, f"{quota.status}, {quota.windows}")
    if "gpt-6-astra" in models:  # the routing's first choice for ops and algorithms
        result = codex.CodexRun(cli, prompt="Réponds juste : OK", workdir=folder, model="gpt-6-astra",
                                permission_mode="plan", timeout_s=300).run()
        check("GPT-6 Astra répond", result.status == "ok" and "ok" in result.text.lower(),
              f"{result.error or result.text[:40]!r} ; {measures(result)}")


def files(cli, folder):
    asked = []
    target = Path(folder) / "essai.txt"
    result = run(cli, folder, "Crée le fichier essai.txt contenant le mot bonjour, puis dis « fait ».",
                 ask_permission=lambda r: asked.append(r) or Decision(True))
    check("écriture demandée puis faite", target.is_file() and "bonjour" in target.read_text(encoding="utf-8"),
          f"demandes : {[a.tool_name for a in asked]} ; {measures(result)}")
    check("la demande montre le fichier ou la commande", asked and (asked[0].input.get("file_path")
                                                                     or asked[0].input.get("command")),
          repr(asked[0].input if asked else None)[:200])
    refused = Path(folder) / "refus.txt"
    result = run(cli, folder, "Crée le fichier refus.txt contenant non.", ask_permission=lambda r: Decision(False))
    check("écriture refusée : rien n'est écrit", not refused.exists() and result.status == "ok",
          f"{result.permission_denials} ; {result.text[:120]!r}")
    first = run(cli, folder, "Retiens le mot de passe d'essai : girafe. Réponds juste « noté ».",
                permission_mode="plan")
    again = run(cli, folder, "Quel mot t'ai-je demandé de retenir ? Un seul mot.", permission_mode="plan",
                resume_session=first.session_id)
    check("session reprise", "girafe" in again.text.lower() and again.session_id == first.session_id,
          f"{again.text[:80]!r} ; {measures(again)}")


def project(folder, with_claude):
    conn = db.connect(str(Path(folder) / "hub.db"))
    try:
        gpt = store.ensure_agent(conn, "codex", MODEL)
        pid = store.create_project(conn, "Boutique")
        other = store.create_conversation(conn, "Charte graphique", agent_id=gpt, workdir=folder, project_id=pid)
        store.add_user_message(conn, other, "Pour le logo, on part sur du vert sapin. C'est décidé.")
        mid = store.start_request(conn, other, gpt)
        store.mark_message_running(conn, mid)
        store.finish_message(conn, mid, "ok", content="Noté : logo vert sapin.")
        here = store.create_conversation(conn, "Site", agent_id=gpt, workdir=folder, project_id=pid)
        result = Request(conn, here, gpt, "Dans la conversation sur la charte graphique, quelle teinte "
                                          "a-t-on retenue pour l'emblème ? Réponds en une phrase.",
                         ask_permission=lambda r: Decision(False, "lecture seule")).run()
        check("question sur une autre conversation du projet", "sapin" in result.text.lower(),
              f"{result.text[:120]!r} ; {measures(result)}")
        # Named nowhere and sharing no word with it: only the hub's MCP tool finds it,
        # in manual mode, where nobody is there to allow it (it must not need anyone).
        asked = []
        result = Request(conn, here, gpt, "Appelle l'outil search_project du serveur aihub avec la requête « logo », "
                                          "puis donne la couleur trouvée en un mot.",
                         ask_permission=lambda r: asked.append(r.tool_name) or Decision(False, "lecture seule")).run()
        check("l'outil MCP du hub marche sous Codex, sans validation",
              "mcp__aihub__search_project" in result.tool_calls and "sapin" in result.text.lower() and not asked,
              f"{result.text[:80]!r} ; demandes {asked} ; {measures(result)}")
        if not with_claude:
            return
        haiku = store.ensure_agent(conn, "claude_code", "haiku")
        cid = store.create_conversation(conn, "Passage de relais", agent_id=haiku, workdir=folder)
        settings = config.validate({"summary_model": "aucun"})
        first = Request(conn, cid, haiku, "Retiens que la réunion est jeudi à 14 h. Réponds juste « noté ».",
                        settings=settings).run()
        check("Claude répond", first.status == "ok", first.error or "")
        then = Request(conn, cid, gpt, "Quel jour et à quelle heure est la réunion ?", settings=settings).run()
        check("Codex reprend avec la mémoire", then.fresh_start == "switched" and "jeudi" in then.text.lower(),
              f"{then.fresh_start} ; {then.text[:100]!r} ; {measures(then)}")
    finally:
        conn.close()


def main():
    with_claude = "--sans-claude" not in sys.argv
    cli = codex.find_cli()
    with tempfile.TemporaryDirectory() as folder:
        free_readings(cli, folder)
        files(cli, folder)
        project(folder, with_claude)
        quota = codex.read_usage(cli, workdir=folder)
        print(f"   quota après l'essai : {quota.windows}")
    print(f"\n{sum(results)}/{len(results)} vérifications réussies")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
