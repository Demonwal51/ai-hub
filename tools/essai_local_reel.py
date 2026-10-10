"""End-to-end check of the local AI on the real local Ollama (free, nothing leaves the PC).

Ollama must run (ollama serve); the hub never starts it.

    .venv\\Scripts\\python -B tools/essai_local_reel.py [modèle]
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aihub import config, db, store  # noqa: E402
from aihub.providers import ollama  # noqa: E402
from aihub.runner import Request  # noqa: E402

MODEL = sys.argv[1] if len(sys.argv) > 1 else "local:qwen3.5:4b"
results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"[{'OK  ' if ok else 'ÉCHEC'}] {name}" + (f" — {detail}" if detail else ""), flush=True)


def measures(r):
    return f"{r.tokens_in} tokens envoyés, {r.tokens_out} reçus, {r.num_turns} appel(s), outils {r.tool_calls}, " \
           f"{r.duration_ms / 1000:.1f} s"


def main():
    command = ollama.find_cli()
    models = ollama.list_models(command)
    check(f"{MODEL} dans Ollama", MODEL in models, ", ".join(models))
    settings = config.validate({"summary_model": "aucun"})
    with tempfile.TemporaryDirectory() as folder:
        Path(folder, "notes.txt").write_text("Le mot de passe du wifi de la cabane est : marmotte.", encoding="utf-8")
        conn = db.connect(str(Path(folder) / "hub.db"))
        try:
            agent = store.ensure_agent(conn, "ollama", MODEL)
            cid = store.create_conversation(conn, "Local", agent_id=agent, workdir=folder)

            def ask(prompt):
                return Request(conn, cid, agent, prompt, settings=settings, timeout_s=600).run()

            first = ask("Réponds juste : bonjour.")
            check("une réponse", first.status == "ok" and first.text.strip(), f"{first.text[:60]!r} ; {measures(first)}")
            read = ask("Lis le fichier notes.txt du dossier et dis quel est le mot de passe du wifi, en un mot.")
            check("lit un fichier du dossier", "marmotte" in read.text.lower() and "read_file" in read.tool_calls,
                  f"{read.text[:80]!r} ; {measures(read)}")
            ask("Retiens ceci : la réunion est jeudi à 14 h. Réponds juste « noté ».")
            later = ask("Quel jour est la réunion ? Un mot.")
            check("se souvient par la mémoire (sans session)", "jeudi" in later.text.lower(),
                  f"{later.text[:60]!r} ; {measures(later)}")
            refused = ask("Crée un fichier nouveau.txt dans le dossier.")
            check("ne modifie rien", not Path(folder, "nouveau.txt").exists(), f"{refused.text[:100]!r}")
        finally:
            conn.close()
    print(f"\n{sum(results)}/{len(results)} vérifications réussies")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
