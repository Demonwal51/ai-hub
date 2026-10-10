"""What an agent receives to read the other conversations of its project.

For a conversation in a project, and shared, the agent is given the
read-only MCP server of aihub/project_mcp.py. Its three tools only read, so
they are allowed without asking the user, in every permission mode.
"""

import os
import sys
from pathlib import Path

from . import project_mcp

SERVER_SCRIPT = Path(project_mcp.__file__).resolve()
TOOL_PREFIX = f"mcp__{project_mcp.SERVER}__"
TOOL_NAMES = [TOOL_PREFIX + name for name in project_mcp.TOOL_NAMES]


def _python():
    """pythonw on Windows: the server gets no console window of its own."""
    exe = Path(sys.executable)
    if os.name == "nt":
        windowless = exe.with_name("pythonw.exe")
        if windowless.exists():
            return str(windowless)
    return str(exe)


def database_path(conn):
    row = conn.execute("PRAGMA database_list").fetchone()
    path = row[2] if row is not None else ""
    return path or None  # "" for an in-memory database


CATALOG = 15          # conversations named in the instruction, the most recent first
PREFETCH_BUDGET = 4000  # characters of passages joined to a request that names conversations
PREFETCH_MAX = 2      # conversations read in advance for one request
AUTO_BUDGET = 1500    # characters joined when the request names no conversation


def instructions(conn, conversation):
    """What the agent may consult, for its system prompt, or None.

    The list of the other conversations comes with it: the agent knows what
    exists without a tool call (each call sends the whole context again).
    """
    if mcp_config(conn, conversation) is None:
        return None
    project = conn.execute("SELECT name FROM projects WHERE id = ?", (conversation["project_id"],)).fetchone()
    name = project["name"] if project is not None else "?"
    others = conn.execute(
        "SELECT id, title FROM conversations WHERE project_id = ? AND shared = 1 AND id <> ? "
        "ORDER BY updated_at DESC, id DESC LIMIT ?",
        (conversation["project_id"], conversation["id"], CATALOG + 1)).fetchall()
    if others:
        listed = "; ".join(f"#{row['id']} « {row['title']} »" for row in others[:CATALOG])
        listed = f"Autres conversations du projet : {listed}" + (" (et d'autres)." if len(others) > CATALOG else ".")
    else:
        listed = "Le projet n'a pas encore d'autre conversation partagée."
    return (f"Cette conversation fait partie du projet « {name} » dans AI Hub. {listed} "
            "L'outil search_project (serveur aihub) trouve en un appel les passages de ces conversations les "
            "plus proches d'une question ; read_conversation lit une conversation par tranches. Lecture seule. "
            "Ne t'en sers que si la demande parle d'une autre conversation ou d'une décision prise ailleurs dans "
            "le projet, et préfère un seul search_project : chaque appel d'outil coûte des tokens. Si des "
            "extraits sont déjà joints à la demande, réponds d'abord avec eux.")


def prefetch(conn, conversation, prompt, *, automatic=True):
    """Passages of the conversations a request names (« #12 » or a title), to join to it.

    Read here, for free, instead of by the agent: no tool round trip. Only
    what the agent could read itself (same project, shared). "" if none.
    `automatic`: when it names none, join the closest passages of the
    project if they are clearly related (only for what the user typed: the
    hub's own prompts are full of generic words).
    """
    if mcp_config(conn, conversation) is None:
        return ""
    access = project_mcp.Access(database_path(conn), conversation["id"])
    named = list(access.cited(prompt))[:PREFETCH_MAX]
    if named:
        share = PREFETCH_BUDGET // len(named)
        blocks = [block for cid in named if (block := access.relevant(cid, prompt, share))]
        what = "Passages des conversations citées, les plus proches de la demande"
    else:
        # Named nothing: the closest passages of the project, if they share
        # enough words with the request to be worth their tokens.
        found = access.closest(prompt, AUTO_BUDGET) if automatic else ""
        blocks = [found] if found else []
        what = "Passages d'autres conversations du projet qui semblent liés à la demande"
    if not blocks:
        return ""
    return (f"\n\n<extraits_du_projet>\n{what} (lus par AI Hub ; search_project ou read_conversation "
            "pour en savoir plus) :\n\n" + "\n\n".join(blocks) + "\n</extraits_du_projet>")


def mcp_config(conn, conversation, own=False):
    """The --mcp-config for this conversation's agents, or None (no project, not shared).

    `own`: a fresh start, whose agent may search the conversation's own past
    (its history is not resent): the server is given even outside a project.
    """
    if not own and (conversation["project_id"] is None or not conversation["shared"]):
        return None
    db_path = database_path(conn)
    if db_path is None:
        return None
    return {"mcpServers": {project_mcp.SERVER: {
        "type": "stdio",
        "command": _python(),
        # -I: the agent's folder must not be able to plant a module the server would import.
        "args": ["-I", str(SERVER_SCRIPT), "--db", db_path, "--conversation", str(conversation["id"])],
    }}}
