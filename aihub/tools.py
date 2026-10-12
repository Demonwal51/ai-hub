"""The tools an agent gets, chosen per conversation (« Outils »).

- « web » : the AI's own web search (Claude Code's WebSearch and WebFetch,
  Codex's web search). On by default, as before this choice existed.
- « coffre » : the Obsidian vault of the settings, read only
  (aihub/vault_mcp.py), allowed without asking anyone.
- the MCP servers the person lists in the settings (a catalog): given to a
  conversation only when chosen there; a server marked read-only is allowed
  without asking, any other goes through the conversation's permission mode.

The hub's own servers (projects, memory: aihub/project_access.py) come on top.
"""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import project_access, vault_mcp

WEB, VAULT, DELEGATE = "web", "coffre", "deleguer"
DEFAULT = (WEB,)
BUILTIN_LABELS = {WEB: "Recherche web", VAULT: "Coffre Obsidian (lecture seule)",
                  DELEGATE: "Demander l'avis d'une autre IA"}
RESERVED = {WEB, VAULT, DELEGATE, "aihub"}
VAULT_SCRIPT = Path(vault_mcp.__file__).resolve()
DELEGATE_SCRIPT = VAULT_SCRIPT.with_name("delegate_mcp.py")
# Another AI's answer takes minutes: the CLIs' own limit on one tool call is raised.
DELEGATE_TIMEOUT_S = 900
_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,30}$")


class ToolError(Exception):
    """A refused tool or catalog entry; the message is fit for the user."""


def check_catalog(servers):
    """The settings' list of MCP servers, checked: [{name, command, args, description, read_only}]."""
    if not isinstance(servers, list):
        raise ToolError("serveurs d'outils : une liste est attendue")
    out, names = [], set()
    for entry in servers:
        if not isinstance(entry, dict):
            raise ToolError("serveur d'outils illisible")
        unknown = set(entry) - {"name", "command", "args", "description", "read_only"}
        if unknown:
            # No env: a key in it would sit in the settings file, which holds no secret.
            raise ToolError(f"serveur d'outils : champ inconnu {', '.join(sorted(unknown))} (pas de variables "
                            "d'environnement : un serveur qui a besoin d'une clé la lit dans sa propre config)")
        name = entry.get("name")
        if not isinstance(name, str) or not _NAME.match(name) or name in RESERVED:
            raise ToolError(f"nom de serveur refusé : {name!r} (minuscules, chiffres, - et _ ; pas "
                            f"{', '.join(sorted(RESERVED))})")
        if name in names:
            raise ToolError(f"deux serveurs s'appellent {name}")
        names.add(name)
        command = entry.get("command")
        if not isinstance(command, str) or not command.strip() or len(command) > 500:
            raise ToolError(f"serveur {name} : commande vide ou trop longue")
        args = entry.get("args", [])
        if not isinstance(args, list) or len(args) > 50 or any(not isinstance(a, str) or len(a) > 500 for a in args):
            raise ToolError(f"serveur {name} : arguments illisibles (une liste de textes)")
        description = entry.get("description", "")
        if not isinstance(description, str) or len(description) > 200:
            raise ToolError(f"serveur {name} : description de 200 caractères au plus")
        read_only = entry.get("read_only", False)
        if not isinstance(read_only, bool):
            raise ToolError(f"serveur {name} : lecture seule oui ou non")
        out.append({"name": name, "command": command.strip(), "args": list(args),
                    "description": description.strip(), "read_only": read_only})
    return out


def offered(settings):
    """What a conversation may choose: [{name, label, description, read_only}]."""
    items = [{"name": WEB, "label": BUILTIN_LABELS[WEB], "read_only": True,
              "description": "La recherche web de l'IA elle-même (Claude Code, Codex)."}]
    items.append({"name": DELEGATE, "label": BUILTIN_LABELS[DELEGATE], "read_only": True,
                  "description": "L'IA peut demander un avis à une autre IA (qui lit le dossier sans rien "
                                 "modifier) : 3 fois au plus par demande, chacune sur le quota de l'autre."})
    if settings.obsidian_vault:
        items.append({"name": VAULT, "label": BUILTIN_LABELS[VAULT], "read_only": True,
                      "description": f"Lire et chercher les notes de {settings.obsidian_vault}."})
    for server in settings.tool_servers:
        items.append({"name": server["name"], "label": server["name"], "read_only": server["read_only"],
                      "description": server["description"]})
    return items


def chosen(conversation):
    raw = conversation["tools_json"]
    if raw is None:
        return list(DEFAULT)
    try:
        value = json.loads(raw)
    except ValueError:
        return list(DEFAULT)
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else list(DEFAULT)


def check_choice(names, settings):
    if not isinstance(names, list) or any(not isinstance(n, str) for n in names):
        raise ToolError("outils : une liste de noms est attendue")
    known = {item["name"] for item in offered(settings)}
    unknown = [n for n in names if n not in known]
    if unknown:
        raise ToolError(f"outil non proposé : {', '.join(unknown)}")
    return [item["name"] for item in offered(settings) if item["name"] in names]  # the catalog's order


@dataclass
class RunTools:
    mcp_config: dict | None = None
    auto_allow: list = field(default_factory=list)
    web: bool = True
    env: dict = field(default_factory=dict)  # added to the CLI's environment


def delegate_models(settings, *, commands=None, account_env=None):
    """The models a delegation may ask: those of the settings whose provider is connected."""
    from . import accounts, config, routing
    from .providers import claude_code
    if account_env:
        import os
        env = claude_code.clean_env(os.environ)
        for name in accounts.SECRET_NAMES:
            env.pop(name, None)
        env.update(account_env)
        connected = accounts.available_models(commands, env, str(Path.cwd()))
    else:
        connected = routing.available()
    out = []
    for model in settings.models:
        provider = config.provider_of(model)
        if provider not in connected:
            continue
        offered = connected[provider]
        if offered is None or model in offered or model in ("default", "codex"):
            out.append(model)
    return out


def for_run(conn, conversation, settings, *, own=False, commands=None, project=True, account_env=None):
    """The MCP servers, the tools allowed without asking, and the web search, of one run.

    `commands`: the CLIs the hub was given (tests), passed on to a delegation.
    `account_env`: the variables naming the asking person's own AI accounts
    (aihub/accounts.py), given to the delegation too: what it asks is paid
    by that person (Codex hands its MCP servers only a few variables)."""
    names = chosen(conversation)
    base = project_access.mcp_config(conn, conversation, own=own, project=project)
    servers = dict((base or {}).get("mcpServers", {}))
    auto = list(project_access.TOOL_NAMES) if base else []
    if VAULT in names and settings.obsidian_vault:
        servers[VAULT] = {"type": "stdio", "command": project_access._python(),
                          "args": ["-I", str(VAULT_SCRIPT), "--vault", str(Path(settings.obsidian_vault).resolve())]}
        auto += [f"mcp__{VAULT}__{tool}" for tool in vault_mcp.TOOL_NAMES]
    env = {}
    db_path = project_access.database_path(conn)
    models = delegate_models(settings, commands=commands, account_env=account_env) if DELEGATE in names else []
    if DELEGATE in names and db_path and models:
        args = ["-I", str(DELEGATE_SCRIPT), "--db", db_path, "--conversation", str(conversation["id"]),
                "--models", ",".join(models)]
        if commands is not None:
            args += ["--commands", json.dumps(commands if isinstance(commands, dict) else {"claude_code": commands})]
        servers[DELEGATE] = {"type": "stdio", "command": project_access._python(), "args": args,
                             "tool_timeout_sec": DELEGATE_TIMEOUT_S}
        if account_env:
            servers[DELEGATE]["env"] = dict(account_env)
        auto.append(f"mcp__{DELEGATE}__ask_ai")  # chosen by the person; limited and recorded by the server
        env["MCP_TOOL_TIMEOUT"] = str(DELEGATE_TIMEOUT_S * 1000)  # Claude Code's limit, in ms
    catalog = {s["name"]: s for s in settings.tool_servers}
    for name in names:
        server = catalog.get(name)
        if server is None:
            continue  # taken out of the settings since: not given
        servers[name] = {"type": "stdio", "command": server["command"], "args": list(server["args"])}
        if server["read_only"]:
            auto.append(f"mcp__{name}")  # every tool of that server
    return RunTools(mcp_config={"mcpServers": servers} if servers else None, auto_allow=auto, web=WEB in names,
                    env=env)
