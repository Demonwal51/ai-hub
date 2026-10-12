"""Command line for AI Hub, until the web interface exists.

    python -m aihub status
    python -m aihub ask --workdir DOSSIER [--mode manual|plan|bypass] "demande"
    python -m aihub ask --conversation 3 "suite de la demande"
    python -m aihub conversations
    python -m aihub quota
    python -m aihub mot-de-passe
    python -m aihub serve [--sans-mot-de-passe]
"""

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from . import VERSION_LABEL, __version__, db, store
from .providers import claude_code, codex
from .providers.claude_code import Decision, ProviderError
from .runner import Request
from .store import StoreError

DEFAULT_DB = Path(__file__).resolve().parents[1] / "data" / "aihub.db"
DEFAULT_AGENT = "Claude Code"
_INPUT_PREVIEW = 600


def _parser():
    parser = argparse.ArgumentParser(prog="aihub", description="AI Hub en ligne de commande")
    parser.add_argument("--version", action="version", version=f"AI Hub {__version__} ({VERSION_LABEL})")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="fichier de la base (défaut : data/aihub.db)")
    parser.add_argument("--claude", help="chemin de claude.exe, si la recherche automatique échoue")
    parser.add_argument("--codex", help="chemin de codex.exe, si la recherche automatique échoue")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="trouver Claude Code et dire s'il est connecté")

    ask = sub.add_parser("ask", help="envoyer une demande à Claude Code")
    ask.add_argument("prompt", help="la demande")
    ask.add_argument("--conversation", type=int, help="continuer une conversation existante")
    ask.add_argument("--workdir", help="dossier de travail d'une nouvelle conversation")
    ask.add_argument("--mode", choices=store.PERMISSION_MODES, default="manual",
                     help="mode de permission d'une nouvelle conversation (défaut : manual)")
    ask.add_argument("--model", help="modèle de l'agent (défaut : celui de Claude Code)")
    ask.add_argument("--title", help="titre d'une nouvelle conversation")
    ask.add_argument("--timeout", type=int, help="délai maximal en secondes")

    sub.add_parser("conversations", help="lister les conversations")
    quota = sub.add_parser("quota", help="état du quota de Claude Code")
    quota.add_argument("--refresh", action="store_true",
                       help="le relire auprès de Claude Code (ne consomme pas de quota)")

    serve = sub.add_parser("serve", help="lancer l'interface web")
    serve.add_argument("--port", type=int, default=8770)
    serve.add_argument("--host", default="127.0.0.1",
                       help="adresse d'écoute (défaut : 127.0.0.1 ; avec Tailscale, passer par "
                            "« tailscale serve » plutôt que d'ouvrir une autre adresse)")
    # The options below fix a setting for this run, over data/config.json
    # (the Réglages window); left out, the file decides.
    serve.add_argument("--config", metavar="FICHIER",
                       help="fichier des réglages (défaut : config.json à côté de la base)")
    serve.add_argument("--allow", action="append", default=None, metavar="DOSSIER",
                       help="dossier racine autorisé pour les dossiers de travail (répétable)")
    serve.add_argument("--obsidian", metavar="COFFRE",
                       help="coffre Obsidian des notes de projet (ex. D:\\notes\\coffre)")
    serve.add_argument("--sans-mot-de-passe", action="store_true", dest="no_password",
                       help="sans mot de passe : l'appli ne répond qu'à cet ordinateur")
    serve.add_argument("--session-jours", type=float, dest="idle_days",
                       help="une session se ferme après ce nombre de jours sans usage (défaut : 7)")
    serve.add_argument("--essais", type=int, dest="max_failures",
                       help="mots de passe erronés avant blocage (défaut : 5)")
    serve.add_argument("--blocage-minutes", type=float, dest="lock_minutes",
                       help="durée du premier blocage, doublée à chaque nouveau (défaut : 15)")
    serve.add_argument("--sauvegardes", metavar="DOSSIER", dest="backup_dir",
                       help="dossier des sauvegardes quotidiennes (défaut : data/sauvegardes)")
    serve.add_argument("--garder", type=int, dest="backup_keep",
                       help="nombre de sauvegardes quotidiennes gardées (défaut : 14)")
    serve.add_argument("--sans-sauvegarde", action="store_true", dest="no_backup",
                       help="ne pas faire de sauvegarde quotidienne")
    serve.add_argument("--laisser-dormir", action="store_true", dest="let_sleep",
                       help="ne pas empêcher la mise en veille pendant qu'un agent travaille")
    serve.add_argument("--journal", metavar="FICHIER",
                       help="écrire les messages du serveur dans ce fichier (lancement sans fenêtre)")

    sub.add_parser("mot-de-passe", help="définir ou changer le mot de passe (ferme toutes les sessions)")

    startup = sub.add_parser("demarrage-auto", help="lancer le serveur à l'ouverture de la session Windows ou Linux")
    startup.add_argument("action", choices=("activer", "desactiver", "etat"))
    startup.add_argument("options", nargs=argparse.REMAINDER,
                         help="options de serve, après « -- » (ex. -- --obsidian D:\\notes\\coffre)")

    restore = sub.add_parser("restaurer", help="remettre la base d'une sauvegarde quotidienne (serveur arrêté)")
    restore.add_argument("sauvegarde", nargs="?", help="nom ou chemin de la sauvegarde (sans : la liste)")
    restore.add_argument("--sauvegardes", metavar="DOSSIER", dest="backup_dir")
    return parser


# --- the running server, known by its pid file ---------------------------------

def pid_file(db_path):
    return Path(db_path).resolve().parent / "serveur.pid"


def _process_alive(pid):
    if os.name == "nt":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def running_server(db_path):
    """(pid, port) of the server using this database, or None (a stale file is ignored)."""
    try:
        data = json.loads(pid_file(db_path).read_text(encoding="utf-8"))
        pid, port = int(data["pid"]), data.get("port")
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return (pid, port) if pid != os.getpid() and _process_alive(pid) else None


_LOCAL_HOSTS = ("127.0.0.1", "::1", "localhost")


def cmd_password(args, out, secret):
    from . import auth
    first = secret(f"Nouveau mot de passe (au moins {auth.MIN_PASSWORD} caractères) : ")
    if first != secret("Le même, une seconde fois : "):
        out("Les deux saisies diffèrent : rien n'a changé.")
        return 1
    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    conn = db.connect(args.db)
    try:
        auth.set_password(conn, first)
    except auth.AuthError as exc:
        out(f"{exc} : rien n'a changé.")
        return 1
    finally:
        conn.close()
    out("Mot de passe enregistré (seule son empreinte est gardée). Toutes les sessions sont fermées.")
    return 0


def cmd_serve(args, out, command):
    try:
        import uvicorn
        from .web.app import create_app
    except ImportError:
        out("Il manque les dépendances web : pip install -r requirements.txt")
        return 1
    from . import auth
    already = running_server(args.db)
    if already:
        out(f"Un serveur AI Hub tourne déjà sur cette base (processus {already[0]}, port {already[1]}).")
        return 2
    from . import config
    config_path = Path(args.config) if args.config else Path(args.db).resolve().parent / "config.json"
    overrides = {name: value for name, value in (
        ("allowed_roots", args.allow), ("obsidian_vault", args.obsidian), ("idle_days", args.idle_days),
        ("max_failures", args.max_failures), ("lock_minutes", args.lock_minutes),
        ("backup_dir", args.backup_dir), ("backup_keep", args.backup_keep),
        ("backup_enabled", False if args.no_backup else None),
        ("keep_awake", False if args.let_sleep else None)) if value is not None}
    try:
        settings = config.validate(overrides, base=config.load(config_path))
    except config.ConfigError as exc:
        out(f"Réglage refusé : {exc}")
        return 1
    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    conn = db.connect(args.db)
    try:
        has_password = auth.password_set(conn)
    finally:
        conn.close()
    if args.no_password:
        if args.host not in _LOCAL_HOSTS:
            out(f"Sans mot de passe, l'appli n'écoute que sur cet ordinateur : --host {args.host} refusé.")
            return 2
        policy = None
    else:
        policy = auth.Policy(idle_days=settings.idle_days, max_failures=settings.max_failures,
                             lock_minutes=settings.lock_minutes)
        if not has_password:
            # A new server: it is created from the page, on this computer only.
            out("Serveur pas encore créé : ouvrez l'adresse ci-dessous sur cet ordinateur pour choisir")
            out("votre nom, votre mot de passe (facultatif) et celui du serveur (facultatif).")
    codex_cli = codex.find_cli(args.codex) if args.codex else None
    if codex_cli is None and command is None:  # a test's own Claude Code: no real Codex behind it
        try:
            codex_cli = codex.find_cli()
        except ProviderError:
            codex_cli = None  # not installed: the app says so in its status
    app = create_app(args.db, command=command or (claude_code.find_cli(args.claude) if args.claude else None),
                     auth_policy=policy, local_only=policy is None, settings=settings,
                     config_path=config_path, overridden=sorted(overrides), codex_command=codex_cli,
                     journal_path=args.journal)
    backup_dir = app.state.backup.folder if app.state.backup else None
    if app.state.jobs.recovered:
        out(f"{app.state.jobs.recovered} requête(s) interrompue(s) par un arrêt précédent ont été closes.")
    shown = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    out(f"Interface : http://{shown}:{args.port}/"
        + ("  (mot de passe demandé)" if policy else "  (cet ordinateur seulement, sans mot de passe)"))
    out(f"Sauvegarde quotidienne : {backup_dir}" if backup_dir else "Sauvegarde quotidienne : désactivée")
    out("Mise en veille retenue pendant qu'un agent travaille." if settings.keep_awake
        else "Mise en veille : laissée libre.")
    out(f"Réglages : {config_path}" + (" (pas encore de fichier : valeurs par défaut)" if not config_path.exists() else "")
        + (f" ; fixés au lancement : {', '.join(sorted(overrides))}" if overrides else ""))
    marker = pid_file(args.db)
    marker.write_text(json.dumps({"pid": os.getpid(), "port": args.port}), encoding="utf-8")
    try:
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    finally:
        try:
            if json.loads(marker.read_text(encoding="utf-8")).get("pid") == os.getpid():
                marker.unlink()
        except (OSError, ValueError):
            pass
    return 0


# --- start with Windows ------------------------------------------------------------

SHORTCUT_NAME = "AI Hub.lnk"
ROOT = Path(__file__).resolve().parents[1]


def startup_folder():
    """The user's Startup folder: a shortcut there runs at each session opening, no admin needed."""
    override = os.environ.get("AIHUB_DEMARRAGE_DOSSIER")  # tests
    if override:
        return Path(override)
    return Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def _windowless_python():
    exe = Path(sys.executable)
    windowless = exe.with_name("pythonw.exe")
    return windowless if windowless.exists() else exe


_SHORTCUT_PS = r"""
$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:AIHUB_LNK)
if ($env:AIHUB_MODE -eq 'write') {
  $s.TargetPath = $env:AIHUB_TARGET
  $s.Arguments = $env:AIHUB_ARGS
  $s.WorkingDirectory = $env:AIHUB_WORKDIR
  $s.WindowStyle = 7
  $s.Description = 'AI Hub : serveur lancé à l''ouverture de session'
  $s.Save()
} else {
  [Console]::OutputEncoding = [Text.Encoding]::UTF8
  Write-Output $s.TargetPath
  Write-Output $s.Arguments
}
"""


def _shortcut(mode, path, target="", arguments="", workdir=""):
    # Values go through the environment: no quoting to get wrong in a command line.
    env = dict(os.environ, AIHUB_MODE=mode, AIHUB_LNK=str(path), AIHUB_TARGET=str(target),
               AIHUB_ARGS=arguments, AIHUB_WORKDIR=str(workdir))
    proc = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", _SHORTCUT_PS],
                          capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
                          creationflags=claude_code.NO_WINDOW, timeout=60)
    if proc.returncode != 0:
        raise OSError(proc.stderr.strip() or f"powershell a échoué (code {proc.returncode})")
    return proc.stdout.splitlines()


def cmd_startup(args, out):
    if os.name != "nt":
        from .linux import startup
        return startup(args, out)
    shortcut = startup_folder() / SHORTCUT_NAME
    if args.action == "activer":
        options = [o for o in args.options if o != "--"]
        journal = Path(args.db).resolve().parent / "serveur.log"
        line = subprocess.list2cmdline(["-m", "aihub", "--db", str(Path(args.db).resolve()), "serve",
                                        "--journal", str(journal), *options])
        try:
            shortcut.parent.mkdir(parents=True, exist_ok=True)
            _shortcut("write", shortcut, _windowless_python(), line, ROOT)
        except (OSError, subprocess.SubprocessError) as exc:
            out(f"Raccourci impossible à créer : {exc}")
            return 1
        out(f"Démarrage automatique activé : {shortcut}")
        out(f"  {_windowless_python()} {line}")
        out(f"Le serveur démarrera sans fenêtre à la prochaine ouverture de session ; ses messages vont dans {journal}.")
        return 0
    if args.action == "desactiver":
        if shortcut.exists():
            shortcut.unlink()
            out("Démarrage automatique désactivé (le serveur en cours, s'il y en a un, continue).")
        else:
            out("Le démarrage automatique n'était pas activé.")
        return 0
    if shortcut.exists():
        try:
            target, *rest = _shortcut("read", shortcut) or [""]
        except (OSError, subprocess.SubprocessError) as exc:
            target, rest = f"(illisible : {exc})", []
        out(f"Démarrage automatique : activé ({shortcut})")
        out(f"  {target} {rest[0] if rest else ''}".rstrip())
    else:
        out("Démarrage automatique : désactivé")
    server = running_server(args.db)
    out(f"Serveur : en marche (processus {server[0]}, port {server[1]})" if server else "Serveur : arrêté")
    return 0


# --- restore a backup ---------------------------------------------------------------

def cmd_restore(args, out, ask):
    from . import backup
    folder = Path(args.backup_dir) if args.backup_dir else Path(args.db).resolve().parent / "sauvegardes"
    copies = backup.backups(folder)
    if not args.sauvegarde:
        if not copies:
            out(f"Aucune sauvegarde dans {folder}.")
            return 1
        out(f"Sauvegardes dans {folder} (la plus récente en dernier) :")
        for path in copies:
            out(f"  {path.name}  ({path.stat().st_size // 1024} Ko)")
        out("Pour en remettre une : python -m aihub restaurer aihub-AAAA-MM-JJ.db")
        return 0
    chosen = Path(args.sauvegarde)
    if not chosen.is_absolute() and not chosen.exists():
        chosen = folder / chosen
    if not chosen.is_file():
        out(f"Sauvegarde introuvable : {chosen}")
        return 1
    server = running_server(args.db)
    if server:
        out(f"Le serveur tourne (processus {server[0]}) : arrêtez-le avant de restaurer.")
        return 2
    try:
        check = sqlite3.connect(f"{chosen.resolve().as_uri()}?mode=ro", uri=True)
        try:
            if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise sqlite3.DatabaseError("contrôle d'intégrité en échec")
            conversations = check.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
        finally:
            check.close()
    except sqlite3.Error as exc:
        out(f"Cette sauvegarde est illisible : {exc}")
        return 1
    out(f"La base actuelle sera remplacée par {chosen.name} ({conversations} conversation(s)).")
    out("Ce qui a été fait depuis cette sauvegarde sera perdu ; la base actuelle est d'abord copiée à côté.")
    if ask("Tapez « oui » pour restaurer : ").strip().lower() != "oui":
        out("Rien n'a changé.")
        return 2
    db_path = Path(args.db).resolve()
    keep = db_path.with_name(f"avant-restauration-{datetime.now():%Y-%m-%d_%H%M%S}.db")
    try:
        restored_files = backup.restore_database(chosen, db_path, keep)
    except (OSError, sqlite3.Error, backup.BackupError) as exc:
        out(f"Restauration impossible : {exc}" + (f" (la base d'avant est dans {keep})" if keep.exists() else ""))
        return 1
    out(f"Base restaurée depuis {chosen.name}." + (f" L'ancienne est gardée : {keep}" if keep.exists() else ""))
    if restored_files:
        out(f"{restored_files} pièce(s) jointe(s) restaurée(s).")
    return 0


def _format_input(tool_input):
    text = json.dumps(tool_input, ensure_ascii=False, indent=2)
    if len(text) > _INPUT_PREVIEW:
        text = text[:_INPUT_PREVIEW] + f"\n… ({len(text) - _INPUT_PREVIEW} caractères de plus)"
    return text


def _local(iso):
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%d/%m/%Y %H:%M")
    except (TypeError, ValueError):
        return iso


def _agent(conn, model):
    for agent in store.list_agents(conn):
        if agent["name"] == DEFAULT_AGENT:
            if model and agent["model"] != model:
                store.update_agent(conn, agent["id"], model=model)
            return agent["id"]
    return store.create_agent(conn, DEFAULT_AGENT, claude_code.PROVIDER,
                              model or claude_code.DEFAULT_MODEL)


def cmd_status(args, out, command):
    try:
        cli = command or claude_code.find_cli(args.claude)
    except ProviderError as exc:
        out(str(exc))
        return 1
    out(f"Claude Code : {' '.join(cli)}")
    proc = subprocess.run(cli + ["auth", "status"], capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, timeout=60,
                          env=claude_code.clean_env(os.environ),
                          encoding="utf-8", errors="replace", creationflags=claude_code.NO_WINDOW)
    try:
        status = json.loads(proc.stdout)
    except json.JSONDecodeError:
        out("État de connexion illisible :")
        out(proc.stdout.strip() or proc.stderr.strip())
        return 1
    if status.get("loggedIn"):
        out(f"Connecté ({status.get('authMethod', '?')}).")
        return 0
    out("Non connecté. Lancez cette commande dans un terminal, puis connectez-vous dans le navigateur :")
    out(f'  "{cli[0]}" auth login')
    return 1


def cmd_ask(args, conn, ask, out, command):
    if args.conversation is not None:
        conversation = store.get_conversation(conn, args.conversation)
        cid = conversation["id"]
        agent_id = conversation["agent_id"] or _agent(conn, args.model)
    else:
        if not args.workdir:
            out("Indiquez --workdir pour une nouvelle conversation.")
            return 2
        workdir = Path(args.workdir).resolve()
        if not workdir.is_dir():
            out(f"Dossier de travail introuvable : {workdir}")
            return 2
        confirm = False
        if args.mode == "bypass":
            out(store.BYPASS_WARNING)
            confirm = ask("Tapez « oui » pour confirmer : ").strip().lower() == "oui"
            if not confirm:
                out("Mode sans permission non confirmé : rien n'a été lancé.")
                return 2
        agent_id = _agent(conn, args.model)
        title = args.title or args.prompt.strip().splitlines()[0][:60]
        cid = store.create_conversation(conn, title, agent_id=agent_id, workdir=str(workdir),
                                        permission_mode=args.mode, confirm_bypass=confirm)
        out(f"Conversation {cid} créée ({args.mode}, dossier {workdir}).")

    def permission(request):
        out("")
        out(f"L'agent demande à utiliser « {request.tool_name} » :")
        out(_format_input(request.input))
        answer = ask("Autoriser ? [o/N] ").strip().lower()
        if answer in ("o", "oui", "y", "yes"):
            return Decision(True)
        reason = ask("Raison du refus (facultatif) : ").strip()
        return Decision(False, reason or "refusé par l'utilisateur")

    request = Request(conn, cid, agent_id, args.prompt, ask_permission=permission,
                      on_text=lambda chunk: out(chunk, end=""), command=command,
                      timeout_s=args.timeout)
    try:
        result = request.run()
    except KeyboardInterrupt:
        out("\nRequête arrêtée.")
        return 130
    out("")
    seconds = result.duration_ms / 1000
    size = (f", {result.tokens_in} tokens envoyés, {result.tokens_out} reçus"
            if result.tokens_in is not None else "")
    if result.status == "ok":
        out(f"[ok en {seconds:.1f} s{size}]")
    elif result.status == "stopped":
        out(f"[arrêtée après {seconds:.1f} s]")
    else:
        out(f"[erreur après {seconds:.1f} s] {result.error}")
    if result.quota and result.quota.status != "allowed":
        _print_quota(out, store.latest_quota(conn, claude_code.PROVIDER))
    out(f"Pour continuer : python -m aihub ask --conversation {cid} \"...\"")
    return 0 if result.status == "ok" else 1


_WINDOW_NAMES = {"five_hour": "5 heures", "seven_day": "7 jours", "seven_day_opus": "7 jours (Opus)",
                 "seven_day_sonnet": "7 jours (Sonnet)"}
_QUOTA_LABELS = {"allowed": "disponible", "allowed_warning": "bientôt atteint", "rejected": "atteint"}


def _percent(value):
    # The CLI reports a fraction (0.59 = 59 %).
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    return f"{value * 100:.0f} %" if value <= 1 else f"{value:.0f} %"


def _percent_left(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    used = value * 100 if value <= 1 else value
    return f"{max(0.0, 100 - used):.0f} %"


def _print_quota(out, quota):
    if quota is None:
        out("Quota de Claude Code : inconnu (il est connu après une première requête).")
        return
    out(f"Quota de Claude Code : {_QUOTA_LABELS.get(quota['status'], quota['status'])}"
        f" — relevé le {_local(quota['observed_at'])}.")
    try:
        windows = json.loads(quota["windows_json"]) if quota["windows_json"] else {}
    except ValueError:
        windows = {}
    if not windows and quota["limit_type"]:
        windows = {quota["limit_type"]: {"utilization": quota["utilization"],
                                         "resets_at": quota["resets_at"]}}
    for name, window in windows.items():
        line = f"  fenêtre {_WINDOW_NAMES.get(name, name)}"
        used = _percent(window.get("utilization"))
        left = _percent_left(window.get("utilization"))
        if used:
            line += f" : reste {left} ({used} utilisés)"
        if window.get("resets_at"):
            line += f", retour le {_local(window['resets_at'])}"
        out(line)


def cmd_conversations(conn, out):
    rows = store.list_conversations(conn)
    if not rows:
        out("Aucune conversation.")
    for row in rows:
        out(f"{row['id']:>4}  {_local(row['updated_at'])}  {row['permission_mode']:<7}  {row['title']}")
    return 0


def main(argv=None, *, ask=input, out=None, command=None, secret=None):
    if out is None:
        def out(text="", end="\n"):
            print(text, end=end, flush=True)
    args = _parser().parse_args(argv)
    if args.command == "status":
        return cmd_status(args, out, command)
    if args.command == "serve":
        if not args.journal:
            return cmd_serve(args, out, command)
        # Started without a window (pythonw): nowhere to print otherwise.
        saved = sys.stdout, sys.stderr
        with open(args.journal, "a", encoding="utf-8", buffering=1) as stream:
            sys.stdout = sys.stderr = stream
            try:
                stream.write(f"--- {datetime.now():%d/%m/%Y %H:%M:%S} : démarrage\n")
                return cmd_serve(args, lambda text="", end="\n": stream.write(text + end), command)
            finally:
                sys.stdout, sys.stderr = saved
    if args.command == "mot-de-passe":
        if secret is None:
            import getpass
            secret = getpass.getpass  # typed without being shown
        return cmd_password(args, out, secret)
    if args.command == "demarrage-auto":
        return cmd_startup(args, out)
    if args.command == "restaurer":
        return cmd_restore(args, out, ask)
    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    conn = db.connect(args.db)
    try:
        if args.command == "ask":
            return cmd_ask(args, conn, ask, out, command or (
                claude_code.find_cli(args.claude) if args.claude else None))
        if args.command == "conversations":
            return cmd_conversations(conn, out)
        if args.command == "quota":
            if args.refresh:
                cli = command or claude_code.find_cli(args.claude)
                info = claude_code.read_usage(cli, workdir=str(Path(args.db).parent))
                store.record_quota(conn, claude_code.PROVIDER, info.status,
                                   limit_type=info.limit_type, utilization=info.utilization,
                                   resets_at=info.resets_at, windows_json=json.dumps(info.windows))
            _print_quota(out, store.latest_quota(conn, claude_code.PROVIDER))
            return 0
    except (StoreError, ProviderError) as exc:
        out(f"\n{exc}")
        return 1
    finally:
        conn.close()
    return 2


if __name__ == "__main__":
    sys.exit(main())
