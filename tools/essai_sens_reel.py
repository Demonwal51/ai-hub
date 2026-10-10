"""End-to-end check of the search by meaning with the real nomic-embed-text of the local Ollama.

A conversation holds decisions; questions that share no word with them must
find them. Free (local), nothing leaves the PC.

    .venv\\Scripts\\python -B tools/essai_sens_reel.py
"""

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aihub import db, memory, semantic, store  # noqa: E402
from aihub.providers import ollama  # noqa: E402

SAID = ["Pour le logo, on a choisi la couleur vert sapin. C'est définitif.",
        "Les marges : 24 px sur ordinateur, 16 px sur téléphone.",
        "La réunion avec le client est jeudi à 14 h.",
        "On refait le site de la boutique, en trois pages : accueil, catalogue, contact."]
ASKED = [("quelle teinte pour l'emblème ?", "vert sapin"), ("espacements sur mobile", "16 px"),
         ("rendez-vous avec le client", "jeudi"), ("combien de rubriques sur le site web", "trois pages")]
results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"[{'OK  ' if ok else 'ÉCHEC'}] {name}" + (f" — {detail}" if detail else ""), flush=True)


def main():
    url = ollama.find_cli()
    models = ollama.embedding_models(url)
    check("modèle d'embeddings dans Ollama", models, ", ".join(models))
    semantic.set_embedder(semantic.Embedder(url[0], models[0]))
    with tempfile.TemporaryDirectory() as folder:
        path = str(Path(folder) / "hub.db")
        conn = db.connect(path)
        try:
            agent = store.create_agent(conn, "Claude", "claude_code", "haiku")
            cid = store.create_conversation(conn, "Boutique", agent_id=agent, workdir=folder)
            for text in SAID:
                store.add_user_message(conn, cid, text)
            started = time.monotonic()
            made = semantic.Indexer(path).tick()
            check("vecteurs faits", made == len(SAID), f"{made} en {time.monotonic() - started:.2f} s")
            for question, expected in ASKED:
                started = time.monotonic()
                found = memory.passages(conn, cid, question, 300)
                first = found[0]["content"] if found else ""
                check(f"« {question} »", expected in first,
                      f"{first[:60]!r} ({time.monotonic() - started:.2f} s)")
        finally:
            conn.close()
    print(f"\n{sum(results)}/{len(results)} vérifications réussies")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
