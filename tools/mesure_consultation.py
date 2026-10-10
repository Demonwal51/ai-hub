"""Measure what a question to another conversation of the project costs, on the real CLI.

Same setting as essai_projets_reel.py (a decision in « Couleurs »), but the
request runs through runner.Request directly, to print the model calls, the
tools called and the CLI's token counts by kind. One Haiku request per case:

    .venv\\Scripts\\python -B tools/mesure_consultation.py [--cite]

--cite: the question names the conversation as the « Citer… » button does.
--vague: it names no conversation, so the agent has to search.
"""

import argparse
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aihub import db, store  # noqa: E402
from aihub.runner import Request  # noqa: E402

DECISION = "vert sapin"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cite", action="store_true")
    parser.add_argument("--vague", action="store_true", help="la question ne nomme aucune conversation")
    parser.add_argument("--sans-ia", action="store_true", dest="sans_ia",
                        help="montrer ce que l'agent recevrait, sans l'appeler (gratuit)")
    parser.add_argument("--env", action="append", default=[], metavar="NOM=VALEUR",
                        help="variable d'environnement donnée à la CLI (essai)")
    args = parser.parse_args()
    import os
    from aihub.providers.claude_code import clean_env
    env = clean_env(os.environ)
    for pair in args.env:
        name, _, value = pair.partition("=")
        env[name] = value
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        conn = db.connect(str(tmp / "hub.db"))
        try:
            agent = store.ensure_agent(conn, "claude_code", "haiku")
            pid = store.create_project(conn, "Boutique")
            colors = store.create_conversation(conn, "Couleurs", agent_id=agent, workdir=str(tmp), project_id=pid)
            # A conversation with some substance, so that reading all of it would cost.
            for i in range(6):
                store.add_user_message(conn, colors, f"Étape {i} : parlons des polices et des marges du site, "
                                                     + "détails sans rapport avec le logo. " * 20)
                mid = store.start_request(conn, colors, agent)
                store.mark_message_running(conn, mid)
                store.finish_message(conn, mid, "ok", content="Noté. " + "Réponse détaillée. " * 40)
                if i == 3:
                    store.add_user_message(conn, colors, f"Pour le logo, on a choisi la couleur {DECISION}.")
                    mid = store.start_request(conn, colors, agent)
                    store.mark_message_running(conn, mid)
                    store.finish_message(conn, mid, "ok", content=f"Noté : le logo sera {DECISION}.")
            home = store.create_conversation(conn, "Accueil", agent_id=agent, workdir=str(tmp), project_id=pid)
            if args.vague:  # names no conversation: the agent must search
                prompt = "Quelle couleur avons-nous choisie pour le logo ? Réponds en une phrase courte."
            else:
                about = (f"la conversation #{colors} « Couleurs »" if args.cite
                         else "la conversation « Couleurs » de ce projet")
                prompt = f"Quelle couleur a été choisie pour le logo dans {about} ? Réponds en une phrase courte."
            if args.sans_ia:  # what the agent would receive, without calling it
                from aihub import project_access, project_mcp
                conv = store.get_conversation(conn, home)
                print("--- joint à la demande ---")
                print(project_access.prefetch(conn, conv, prompt) or "(rien)")
                print("--- search_project(question) ---")
                print(project_mcp.Access(str(tmp / "hub.db"), home).search(prompt))
                return 0
            result = Request(conn, home, agent, prompt, env=env).run()
        finally:
            conn.close()
    found = DECISION in (result.text or "").lower()
    print(f"statut : {result.status} {result.error or ''}")
    print(f"réponse : {result.text!r}")
    print(f"décision retrouvée : {'oui' if found else 'NON'}")
    print(f"appels du modèle : {result.num_turns}, outils : {result.tool_calls}")
    print(f"tokens : {result.usage}")
    print(f"total envoyé : {result.tokens_in}, reçu : {result.tokens_out}, {result.duration_ms / 1000:.1f} s")
    return 0 if result.status == "ok" and found else 1


if __name__ == "__main__":
    sys.exit(main())
