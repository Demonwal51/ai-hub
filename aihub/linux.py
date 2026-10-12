"""Linux desktop integration, per user, without root or global changes."""

import argparse
import os
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def desktop_quote(value):
    # Desktop Entry Exec has its own escaping (including literal percent).
    if any(ord(char) < 32 for char in str(value)):
        raise ValueError('un chemin ou une option contient un caractère de contrôle')
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('`', '\\`').replace('$', '\\$').replace('%', '%%') + '"'


def desktop_entry(root=ROOT, *, autostart=False, db=None, options=()):
    root = Path(root).resolve()
    desktop_quote(root)
    python = root / '.venv/bin/python'
    if autostart:
        database = Path(db).resolve()
        command = [str(python), '-m', 'aihub', '--db', str(database), 'serve',
                   '--journal', str(database.parent / 'serveur.log'), *options]
    else:
        command = [str(python), '-m', 'aihub.linux', 'launch']
    return ('[Desktop Entry]\nType=Application\nName=AI Hub\n'
            'Comment=Vos conversations avec les IA\nTerminal=false\nCategories=Office;Development;\n'
            f'Path={root}\nIcon={root / "aihub/web/static/icon-512.png"}\n'
            f'Exec={" ".join(desktop_quote(arg) for arg in command)}\n')


def write_entry(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(content, encoding='utf-8')
    temporary.chmod(0o600)
    os.replace(temporary, path)


def startup(args, out):
    base = Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config')
    path = base / 'autostart/ai-hub.desktop'
    if args.action == 'activer':
        write_entry(path, desktop_entry(autostart=True, db=args.db,
                                       options=[o for o in args.options if o != '--']))
        out('Démarrage automatique activé pour votre session Linux.')
    elif args.action == 'desactiver':
        path.unlink(missing_ok=True)
        out('Démarrage automatique désactivé.')
    else:
        out('Démarrage automatique : ' + ('activé' if path.exists() else 'désactivé'))
    return 0


def launch(root=ROOT):
    from .cli import running_server
    root = Path(root)
    db = root / 'data/aihub.db'
    db.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        port = int(os.environ.get('AIHUB_PORT', '8770'))
        if not 1 <= port <= 65535:
            raise ValueError
    except ValueError:
        raise SystemExit('AIHUB_PORT doit être un port entre 1 et 65535.') from None
    existing = running_server(db)
    if existing:
        port = existing[1]
    else:
        with (db.parent / 'lancement.log').open('a', encoding='utf-8') as log:
            child = subprocess.Popen([sys.executable, '-m', 'aihub', '--db', str(db), 'serve',
                                      '--port', str(port), '--journal', str(db.parent / 'serveur.log')],
                                     cwd=root, start_new_session=True, stdin=subprocess.DEVNULL,
                                     stdout=log, stderr=log)
        url = f'http://127.0.0.1:{port}/'
        for _ in range(60):
            if child.poll() is not None:
                raise SystemExit('Le serveur ne démarre pas. Consultez data/serveur.log et data/lancement.log.')
            try:
                with urllib.request.urlopen(url, timeout=0.5):
                    break
            except OSError:
                time.sleep(0.25)
        else:
            raise SystemExit('Le serveur tarde à démarrer. Consultez data/serveur.log.')
    if os.environ.get('AIHUB_SANS_NAVIGATEUR') != '1':
        webbrowser.open(f'http://127.0.0.1:{port}/')
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('desktop', 'launch'))
    args = parser.parse_args(argv)
    if args.action == 'launch':
        return launch()
    base = Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local/share')
    write_entry(base / 'applications/ai-hub.desktop', desktop_entry())
    return 0


if __name__ == '__main__':
    sys.exit(main())
