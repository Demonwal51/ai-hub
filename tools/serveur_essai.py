"""Web interface on the fake Claude Code CLI, to try the interface for free.

The scenario follows the prompt: "écris" asks for a permission, "question"
asks a question, "attends" hangs (to try the stop button), "connexion" and
"quota" show errors, anything else answers "Bonjour".

    python -B tools/serveur_essai.py [--port 8766] [--mot-de-passe]

Its database is data/essai.db, separate from the real one. With
--mot-de-passe, the login screen is on, with a throwaway test password kept
in data/essai-mot-de-passe.txt (made at the first run), and a lockout of
3 tries for 1 minute so it can be tried quickly.
"""

import argparse
import os
import secrets
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import uvicorn  # noqa: E402

from aihub import auth, config, db  # noqa: E402
from aihub.web.app import create_app  # noqa: E402
from tests.helpers import FAKE_CODEX, FAKE_COMMAND  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--mot-de-passe", action="store_true", dest="password")
    parser.add_argument("--neuf", action="store_true", dest="fresh",
                        help="un serveur pas encore créé (data/essai-neuf.db, recréée à chaque lancement)")
    args = parser.parse_args()
    policy = None
    db_path = ROOT / "data" / ("essai-neuf.db" if args.fresh else "essai-mdp.db" if args.password else "essai.db")
    if args.fresh:
        # Only this throwaway trial base, and its WAL files, by their exact names.
        for name in ("essai-neuf.db", "essai-neuf.db-wal", "essai-neuf.db-shm"):
            (ROOT / "data" / name).unlink(missing_ok=True)
        for name in ("essai-neuf-comptes", "essai-neuf-espaces"):  # its people's trial folders
            shutil.rmtree(ROOT / "data" / name, ignore_errors=True)
        policy = auth.Policy(max_failures=3, lock_minutes=1)
    if args.password:
        secret_file = ROOT / "data" / "essai-mot-de-passe.txt"
        if not secret_file.exists():
            secret_file.write_text(secrets.token_urlsafe(15), encoding="utf-8")
        conn = db.connect(str(db_path))
        try:
            auth.set_password(conn, secret_file.read_text(encoding="utf-8").strip())
        finally:
            conn.close()
        policy = auth.Policy(max_failures=3, lock_minutes=1)
    env = dict(os.environ, FAKE_CLAUDE_SCENARIO="auto", FAKE_CLAUDE_LOGGED_IN="1",
               FAKE_CLAUDE_LOG=str(ROOT / "data" / "essai-fausse-cli.json"))
    # A throwaway vault, so trials never touch a real one.
    vault = ROOT / "data" / "essai-coffre"
    vault.mkdir(parents=True, exist_ok=True)
    # Its own settings file: the Réglages window can be tried without touching the real one.
    config_path = ROOT / "data" / ("essai-mdp-config.json" if args.password else "essai-config.json")
    if not config_path.exists():
        config.save(config_path, config.validate({"obsidian_vault": str(vault),
                                                  "backup_dir": str(ROOT / "data" / "essai-sauvegardes")}))
    settings = config.load(config_path)
    if policy is not None:  # quick lockouts for the trials
        settings = config.validate({"max_failures": 3, "lock_minutes": 1}, base=settings)
    # The fake Codex too (GPT models): « écris », « commande », « quota », « attends », « pense ».
    env.update(FAKE_CODEX_SCENARIO="auto", FAKE_CODEX_MODELS="gpt-5.6-sol,gpt-5.6-terra,gpt-5.6-luna")
    app = create_app(str(db_path), command=FAKE_COMMAND, env=env, auth_policy=policy or None,
                     settings=settings, config_path=config_path, codex_command=FAKE_CODEX,
                     local_only=policy is None)
    print(f"Interface d'essai (fausse CLI) : http://127.0.0.1:{args.port}/", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
