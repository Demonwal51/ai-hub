"""SQLite connection, schema and migrations for AI Hub.

The database enforces the invariants that must hold whatever code calls it:
one active request per conversation, one open plan per conversation, no
"bypass" permission mode without a recorded confirmation, and no change to a
plan once it has been validated.
"""

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

SCHEMA_VERSION = 24

# One statement per string: triggers contain semicolons, so the schema
# cannot be split on ";" and executescript() would commit mid-migration.
_SCHEMA_V1 = [
    """
    CREATE TABLE agents (
        id         INTEGER PRIMARY KEY,
        name       TEXT NOT NULL UNIQUE CHECK (length(trim(name)) > 0),
        provider   TEXT NOT NULL CHECK (provider IN ('claude_code', 'codex', 'ollama')),
        model      TEXT NOT NULL CHECK (length(trim(model)) > 0),
        enabled    INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE conversations (
        id                  INTEGER PRIMARY KEY,
        title               TEXT NOT NULL CHECK (length(trim(title)) > 0),
        mode                TEXT NOT NULL DEFAULT 'single'
                                CHECK (mode IN ('single', 'orchestration')),
        agent_id            INTEGER REFERENCES agents(id) ON DELETE SET NULL,
        conductor_agent_id  INTEGER REFERENCES agents(id) ON DELETE SET NULL,
        permission_mode     TEXT NOT NULL DEFAULT 'manual'
                                CHECK (permission_mode IN ('manual', 'plan', 'bypass')),
        bypass_confirmed_at TEXT,
        assignment_mode     TEXT NOT NULL DEFAULT 'manual'
                                CHECK (assignment_mode IN ('manual', 'auto')),
        workdir             TEXT,
        created_at          TEXT NOT NULL,
        updated_at          TEXT NOT NULL,
        CHECK (permission_mode <> 'bypass' OR bypass_confirmed_at IS NOT NULL)
    )
    """,
    """
    CREATE TABLE messages (
        id              INTEGER PRIMARY KEY,
        conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        role            TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
        agent_id        INTEGER REFERENCES agents(id) ON DELETE SET NULL,
        provider        TEXT,
        model           TEXT,
        content         TEXT NOT NULL DEFAULT '',
        status          TEXT NOT NULL
                            CHECK (status IN ('pending', 'running', 'ok', 'error', 'stopped')),
        error           TEXT,
        duration_ms     INTEGER CHECK (duration_ms IS NULL OR duration_ms >= 0),
        tokens_in       INTEGER CHECK (tokens_in IS NULL OR tokens_in >= 0),
        tokens_out      INTEGER CHECK (tokens_out IS NULL OR tokens_out >= 0),
        created_at      TEXT NOT NULL,
        finished_at     TEXT
    )
    """,
    "CREATE INDEX messages_by_conversation ON messages(conversation_id, id)",
    # The input box is locked while a request runs: at most one active
    # request per conversation, whatever code path tries to start another.
    """
    CREATE UNIQUE INDEX one_active_request ON messages(conversation_id)
        WHERE status IN ('pending', 'running')
    """,
    """
    CREATE TABLE plans (
        id                 INTEGER PRIMARY KEY,
        conversation_id    INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        request_message_id INTEGER REFERENCES messages(id) ON DELETE CASCADE,
        status             TEXT NOT NULL DEFAULT 'proposed'
                               CHECK (status IN ('proposed', 'validated', 'running',
                                                 'done', 'stopped', 'error')),
        created_at         TEXT NOT NULL,
        validated_at       TEXT,
        finished_at        TEXT
    )
    """,
    """
    CREATE UNIQUE INDEX one_open_plan ON plans(conversation_id)
        WHERE status IN ('proposed', 'validated', 'running')
    """,
    """
    CREATE TABLE subtasks (
        id          INTEGER PRIMARY KEY,
        plan_id     INTEGER NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
        position    INTEGER NOT NULL CHECK (position >= 1),
        description TEXT NOT NULL CHECK (length(trim(description)) > 0),
        agent_id    INTEGER REFERENCES agents(id) ON DELETE SET NULL,
        provider    TEXT,
        model       TEXT,
        status      TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'running', 'ok', 'error', 'stopped')),
        result      TEXT,
        error       TEXT,
        duration_ms INTEGER CHECK (duration_ms IS NULL OR duration_ms >= 0),
        tokens_in   INTEGER CHECK (tokens_in IS NULL OR tokens_in >= 0),
        tokens_out  INTEGER CHECK (tokens_out IS NULL OR tokens_out >= 0),
        started_at  TEXT,
        finished_at TEXT,
        UNIQUE (plan_id, position)
    )
    """,
    """
    CREATE TABLE subtask_deps (
        subtask_id    INTEGER NOT NULL REFERENCES subtasks(id) ON DELETE CASCADE,
        depends_on_id INTEGER NOT NULL REFERENCES subtasks(id) ON DELETE CASCADE,
        PRIMARY KEY (subtask_id, depends_on_id),
        CHECK (subtask_id <> depends_on_id)
    )
    """,
    """
    CREATE TRIGGER subtask_deps_same_plan
    BEFORE INSERT ON subtask_deps
    WHEN (SELECT plan_id FROM subtasks WHERE id = NEW.subtask_id)
         IS NOT (SELECT plan_id FROM subtasks WHERE id = NEW.depends_on_id)
    BEGIN
        SELECT RAISE(ABORT, 'dependency across plans');
    END
    """,
    # "Nothing starts without validation": once a plan has left the
    # 'proposed' state, its content (subtasks, assignments, dependencies)
    # is frozen. Only execution fields (status, result, timings) may change.
    """
    CREATE TRIGGER subtasks_frozen_insert
    BEFORE INSERT ON subtasks
    WHEN (SELECT status FROM plans WHERE id = NEW.plan_id) <> 'proposed'
    BEGIN
        SELECT RAISE(ABORT, 'plan is frozen');
    END
    """,
    """
    CREATE TRIGGER subtasks_frozen_delete
    BEFORE DELETE ON subtasks
    WHEN (SELECT status FROM plans WHERE id = OLD.plan_id) NOT IN ('proposed')
         AND EXISTS (SELECT 1 FROM plans WHERE id = OLD.plan_id)
    BEGIN
        SELECT RAISE(ABORT, 'plan is frozen');
    END
    """,
    # The one change allowed on a frozen subtask is agent_id becoming NULL
    # alone: that is the ON DELETE SET NULL cascade when an agent is removed
    # from the configuration. The provider and model the subtask ran with
    # stay recorded.
    """
    CREATE TRIGGER subtasks_frozen_update
    BEFORE UPDATE OF description, agent_id, provider, model, position ON subtasks
    WHEN (SELECT status FROM plans WHERE id = OLD.plan_id) <> 'proposed'
         AND NOT (NEW.agent_id IS NULL
                  AND NEW.description IS OLD.description
                  AND NEW.provider IS OLD.provider
                  AND NEW.model IS OLD.model
                  AND NEW.position IS OLD.position)
    BEGIN
        SELECT RAISE(ABORT, 'plan is frozen');
    END
    """,
    """
    CREATE TRIGGER subtask_deps_frozen_insert
    BEFORE INSERT ON subtask_deps
    WHEN (SELECT p.status FROM plans p JOIN subtasks s ON s.plan_id = p.id
          WHERE s.id = NEW.subtask_id) <> 'proposed'
    BEGIN
        SELECT RAISE(ABORT, 'plan is frozen');
    END
    """,
    """
    CREATE TRIGGER subtask_deps_frozen_delete
    BEFORE DELETE ON subtask_deps
    WHEN (SELECT p.status FROM plans p JOIN subtasks s ON s.plan_id = p.id
          WHERE s.id = OLD.subtask_id) NOT IN ('proposed')
    BEGIN
        SELECT RAISE(ABORT, 'plan is frozen');
    END
    """,
]


_SCHEMA_V2 = [
    # The provider's own session, so the next message of the conversation
    # continues it instead of starting from scratch.
    "ALTER TABLE messages ADD COLUMN session_id TEXT",
    "ALTER TABLE subtasks ADD COLUMN session_id TEXT",
    # Quota state as reported by a provider. Claude Code only reports it
    # after a request has gone through, so the latest row is what is known.
    """
    CREATE TABLE quota_observations (
        id           INTEGER PRIMARY KEY,
        provider     TEXT NOT NULL CHECK (provider IN ('claude_code', 'codex', 'ollama')),
        status       TEXT NOT NULL CHECK (status IN ('allowed', 'allowed_warning', 'rejected')),
        limit_type   TEXT,
        utilization  REAL CHECK (utilization IS NULL OR utilization >= 0),
        resets_at    TEXT,
        windows_json TEXT,
        observed_at  TEXT NOT NULL
    )
    """,
    "CREATE INDEX quota_by_provider ON quota_observations(provider, id)",
    # Every permission asked by an agent and the answer given: the journal of
    # what was allowed in manual and plan modes.
    """
    CREATE TABLE permission_requests (
        id          INTEGER PRIMARY KEY,
        message_id  INTEGER REFERENCES messages(id) ON DELETE CASCADE,
        subtask_id  INTEGER REFERENCES subtasks(id) ON DELETE CASCADE,
        tool_name   TEXT NOT NULL,
        input_json  TEXT NOT NULL,
        decision    TEXT NOT NULL CHECK (decision IN ('allow', 'deny')),
        reason      TEXT,
        decided_at  TEXT NOT NULL,
        CHECK ((message_id IS NULL) <> (subtask_id IS NULL))
    )
    """,
]


_SCHEMA_V3 = [
    # What the conductor proposes: a short title per subtask, the files it
    # will create or change ("*" = the whole project), the plan's summary and
    # how many agents the conductor wants running at once.
    "ALTER TABLE subtasks ADD COLUMN title TEXT",
    "ALTER TABLE subtasks ADD COLUMN files_json TEXT",
    "ALTER TABLE plans ADD COLUMN summary TEXT",
    "ALTER TABLE plans ADD COLUMN max_parallel INTEGER CHECK (max_parallel IS NULL OR max_parallel >= 1)",
    "ALTER TABLE plans ADD COLUMN synthesis_message_id INTEGER REFERENCES messages(id) ON DELETE SET NULL",
    # The frozen-plan rule now covers the new columns too.
    "DROP TRIGGER subtasks_frozen_update",
    """
    CREATE TRIGGER subtasks_frozen_update
    BEFORE UPDATE OF description, agent_id, provider, model, position, title, files_json ON subtasks
    WHEN (SELECT status FROM plans WHERE id = OLD.plan_id) <> 'proposed'
         AND NOT (NEW.agent_id IS NULL
                  AND NEW.description IS OLD.description
                  AND NEW.provider IS OLD.provider
                  AND NEW.model IS OLD.model
                  AND NEW.position IS OLD.position
                  AND NEW.title IS OLD.title
                  AND NEW.files_json IS OLD.files_json)
    BEGIN
        SELECT RAISE(ABORT, 'plan is frozen');
    END
    """,
    """
    CREATE TRIGGER plans_frozen_update
    BEFORE UPDATE OF summary, max_parallel ON plans
    WHEN OLD.status <> 'proposed'
    BEGIN
        SELECT RAISE(ABORT, 'plan is frozen');
    END
    """,
]


_SCHEMA_V4 = [
    # The kind of work the conductor saw in a subtask (code, terminal...) and
    # why the automatic assignment chose its AI (the benchmark it rests on).
    "ALTER TABLE subtasks ADD COLUMN kind TEXT",
    "ALTER TABLE subtasks ADD COLUMN assignment_reason TEXT",
    # Agents the user took out of a plan: neither the conductor nor the
    # table may bring them back, until the user assigns one again.
    "ALTER TABLE plans ADD COLUMN removed_agents_json TEXT",
    "DROP TRIGGER subtasks_frozen_update",
    """
    CREATE TRIGGER subtasks_frozen_update
    BEFORE UPDATE OF description, agent_id, provider, model, position, title, files_json,
                     kind, assignment_reason ON subtasks
    WHEN (SELECT status FROM plans WHERE id = OLD.plan_id) <> 'proposed'
         AND NOT (NEW.agent_id IS NULL
                  AND NEW.description IS OLD.description
                  AND NEW.provider IS OLD.provider
                  AND NEW.model IS OLD.model
                  AND NEW.position IS OLD.position
                  AND NEW.title IS OLD.title
                  AND NEW.files_json IS OLD.files_json
                  AND NEW.kind IS OLD.kind
                  AND NEW.assignment_reason IS OLD.assignment_reason)
    BEGIN
        SELECT RAISE(ABORT, 'plan is frozen');
    END
    """,
]


_SCHEMA_V5 = [
    # The conversation's project note in the Obsidian vault, relative to the
    # vault; NULL while the user has not turned the note on.
    "ALTER TABLE conversations ADD COLUMN obsidian_note TEXT",
    # When the user sent a reply to the note, so a rewrite can put it back.
    "ALTER TABLE messages ADD COLUMN noted_at TEXT",
]


_SCHEMA_V6 = [
    # Access from the phone. The password is kept only as a scrypt hash.
    """
    CREATE TABLE auth_password (
        id            INTEGER PRIMARY KEY CHECK (id = 1),
        password_hash TEXT NOT NULL,
        changed_at    TEXT NOT NULL
    )
    """,
    # A session is known by the SHA-256 of its token: the token itself is only
    # in the browser's cookie, so a copy of the database opens no session.
    """
    CREATE TABLE sessions (
        id           INTEGER PRIMARY KEY,
        token_hash   TEXT NOT NULL UNIQUE,
        created_at   TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        expires_at   TEXT NOT NULL,
        client       TEXT,
        user_agent   TEXT
    )
    """,
    # Wrong passwords, by client address, for the temporary lockout.
    """
    CREATE TABLE login_failures (
        id     INTEGER PRIMARY KEY,
        client TEXT NOT NULL,
        at     TEXT NOT NULL
    )
    """,
    "CREATE INDEX login_failures_by_client ON login_failures(client, at)",
    # A lockout in force; `count` makes each new lockout of a client longer.
    """
    CREATE TABLE login_locks (
        client TEXT PRIMARY KEY,
        until  TEXT NOT NULL,
        count  INTEGER NOT NULL CHECK (count >= 1)
    )
    """,
]


_SCHEMA_V7 = [
    # A project groups conversations. Its folder is offered to the new
    # conversations created in it.
    """
    CREATE TABLE projects (
        id          INTEGER PRIMARY KEY,
        name        TEXT NOT NULL UNIQUE,
        description TEXT,
        workdir     TEXT,
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL
    )
    """,
    # SET NULL: a project removed by other means than the store leaves its
    # conversations without a project, never deletes them behind the user.
    "ALTER TABLE conversations ADD COLUMN project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL",
    # Shared within its project: this conversation's AI may read the other
    # shared conversations of the project, and they may read it. Off: neither.
    "ALTER TABLE conversations ADD COLUMN shared INTEGER NOT NULL DEFAULT 1 CHECK (shared IN (0, 1))",
    "CREATE INDEX conversations_by_project ON conversations(project_id)",
]


_SCHEMA_V8 = [
    # The conversations' memory (RAG): every finished message and every
    # subtask report, indexed by words (accents ignored). Kept up to date by
    # the triggers below, so no code path can forget a message.
    """
    CREATE VIRTUAL TABLE memory USING fts5(
        content, conversation_id UNINDEXED, message_id UNINDEXED, subtask_id UNINDEXED, role UNINDEXED,
        tokenize = 'unicode61 remove_diacritics 2')
    """,
    # Deleted text is overwritten, as in the rest of the base (secure_delete).
    "INSERT INTO memory(memory, rank) VALUES ('secure-delete', 1)",
    """
    CREATE TRIGGER memory_message_insert AFTER INSERT ON messages
    WHEN NEW.content IS NOT NULL AND NEW.content <> '' AND (NEW.role = 'user' OR NEW.status = 'ok')
    BEGIN
        INSERT INTO memory (content, conversation_id, message_id, role)
        VALUES (NEW.content, NEW.conversation_id, NEW.id, NEW.role);
    END
    """,
    # A reply enters the memory when it is finished, not while it is streamed.
    """
    CREATE TRIGGER memory_message_done AFTER UPDATE OF status, content ON messages
    WHEN NEW.status = 'ok' AND NEW.role <> 'user' AND NEW.content IS NOT NULL AND NEW.content <> ''
    BEGIN
        DELETE FROM memory WHERE message_id = NEW.id;
        INSERT INTO memory (content, conversation_id, message_id, role)
        VALUES (NEW.content, NEW.conversation_id, NEW.id, NEW.role);
    END
    """,
    """
    CREATE TRIGGER memory_message_delete AFTER DELETE ON messages
    BEGIN
        DELETE FROM memory WHERE message_id = OLD.id;
    END
    """,
    """
    CREATE TRIGGER memory_subtask_done AFTER UPDATE OF result ON subtasks
    WHEN NEW.result IS NOT NULL AND NEW.result <> ''
    BEGIN
        DELETE FROM memory WHERE subtask_id = NEW.id;
        INSERT INTO memory (content, conversation_id, subtask_id, role)
        SELECT NEW.result, p.conversation_id, NEW.id, 'subtask' FROM plans p WHERE p.id = NEW.plan_id;
    END
    """,
    """
    CREATE TRIGGER memory_subtask_delete AFTER DELETE ON subtasks
    BEGIN
        DELETE FROM memory WHERE subtask_id = OLD.id;
    END
    """,
    # What was already said before this version.
    """
    INSERT INTO memory (content, conversation_id, message_id, role)
    SELECT content, conversation_id, id, role FROM messages
    WHERE content IS NOT NULL AND content <> '' AND (role = 'user' OR status = 'ok')
    """,
    """
    INSERT INTO memory (content, conversation_id, subtask_id, role)
    SELECT s.result, p.conversation_id, s.id, 'subtask' FROM subtasks s JOIN plans p ON p.id = s.plan_id
    WHERE s.result IS NOT NULL AND s.result <> ''
    """,
    # The summary given to a session that starts afresh, written when one
    # is needed, then kept until new messages come.
    """
    CREATE TABLE conversation_summaries (
        conversation_id INTEGER PRIMARY KEY REFERENCES conversations(id) ON DELETE CASCADE,
        content         TEXT NOT NULL,
        up_to_message   INTEGER NOT NULL,
        model           TEXT,
        tokens_in       INTEGER,
        tokens_out      INTEGER,
        created_at      TEXT NOT NULL
    )
    """,
    # The starting package (JSON: summary, last exchanges) of the next
    # request, which then opens a new session fed by the memory instead of
    # resuming the old one (button « Repartir de zéro »). NULL: resume.
    "ALTER TABLE conversations ADD COLUMN fresh_package TEXT",
    # This conversation's own cap for the starting package, in tokens (NULL: the settings').
    "ALTER TABLE conversations ADD COLUMN rag_budget INTEGER CHECK (rag_budget IS NULL OR rag_budget > 0)",
]


_SCHEMA_V9 = [
    # Phone notifications (Web Push). The server's key pair (VAPID): the
    # push services only accept messages signed with the key a device
    # subscribed with. Never shown, never in the settings file.
    """
    CREATE TABLE push_key (
        id          INTEGER PRIMARY KEY CHECK (id = 1),
        private_pem TEXT NOT NULL,
        public_key  TEXT NOT NULL,
        created_at  TEXT NOT NULL
    )
    """,
    # One row per device that asked for notifications, with the alerts it
    # chose. Tied to its login session: a device logged out (or closed from
    # « Appareils ») receives nothing more. NULL session: without a password.
    """
    CREATE TABLE push_subscriptions (
        id          INTEGER PRIMARY KEY,
        endpoint    TEXT NOT NULL UNIQUE,
        p256dh      TEXT NOT NULL,
        auth        TEXT NOT NULL,
        events_json TEXT NOT NULL,
        session_id  INTEGER REFERENCES sessions(id) ON DELETE CASCADE,
        user_agent  TEXT,
        created_at  TEXT NOT NULL,
        last_ok_at  TEXT,
        failures    INTEGER NOT NULL DEFAULT 0
    )
    """,
]


_SCHEMA_V10 = [
    # Autonomous work: a goal the AI pursues alone, round after round (act,
    # the hub checks, it corrects), within limits. Each round's reply is an
    # ordinary message of the conversation; here, how the run went.
    """
    CREATE TABLE autonomous_runs (
        id              INTEGER PRIMARY KEY,
        conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        goal_message_id INTEGER REFERENCES messages(id) ON DELETE SET NULL,
        agent_id        INTEGER REFERENCES agents(id) ON DELETE SET NULL,
        check_command   TEXT,
        max_rounds      INTEGER NOT NULL CHECK (max_rounds BETWEEN 1 AND 50),
        max_minutes     INTEGER NOT NULL CHECK (max_minutes BETWEEN 1 AND 1440),
        status          TEXT NOT NULL CHECK (status IN ('running', 'done', 'limit', 'stopped', 'error')),
        rounds          INTEGER NOT NULL DEFAULT 0,
        summary         TEXT,
        error           TEXT,
        schedule_id     INTEGER,
        started_at      TEXT NOT NULL,
        finished_at     TEXT
    )
    """,
    "CREATE INDEX autonomous_runs_by_conversation ON autonomous_runs(conversation_id, id)",
    """
    CREATE TABLE autonomous_rounds (
        id           INTEGER PRIMARY KEY,
        run_id       INTEGER NOT NULL REFERENCES autonomous_runs(id) ON DELETE CASCADE,
        round        INTEGER NOT NULL,
        message_id   INTEGER REFERENCES messages(id) ON DELETE SET NULL,
        agent_state  TEXT NOT NULL CHECK (agent_state IN ('fini', 'suivre', 'inconnu')),
        check_code   INTEGER,
        check_output TEXT,
        created_at   TEXT NOT NULL
    )
    """,
]


_SCHEMA_V11 = [
    # A request, or autonomous work, that starts again on its own (every
    # day at 8:00, every hour...). Times are kept in UTC; the rule is read
    # in the PC's local time.
    """
    CREATE TABLE schedules (
        id               INTEGER PRIMARY KEY,
        conversation_id  INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        prompt           TEXT NOT NULL,
        kind             TEXT NOT NULL CHECK (kind IN ('single', 'autonomous')),
        options_json     TEXT NOT NULL DEFAULT '{}',
        every            TEXT NOT NULL CHECK (every IN ('interval', 'daily', 'weekly')),
        interval_minutes INTEGER,
        at_time          TEXT,
        weekdays         TEXT,
        enabled          INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
        next_run_at      TEXT NOT NULL,
        last_run_at      TEXT,
        last_status      TEXT,
        last_error       TEXT,
        created_at       TEXT NOT NULL
    )
    """,
    "CREATE INDEX schedules_due ON schedules(enabled, next_run_at)",
]


_SCHEMA_V12 = [
    # The tools chosen for this conversation (JSON list of names, aihub/tools.py). NULL: the default ones.
    "ALTER TABLE conversations ADD COLUMN tools_json TEXT",
]


_SCHEMA_V13 = [
    # Delegation: an agent asked another AI for its opinion while it worked
    # (aihub/delegate_mcp.py). The other AI only read and answered.
    """
    CREATE TABLE delegations (
        id              INTEGER PRIMARY KEY,
        conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        provider        TEXT NOT NULL,
        model           TEXT NOT NULL,
        question        TEXT NOT NULL,
        answer          TEXT,
        status          TEXT NOT NULL CHECK (status IN ('running', 'ok', 'error')),
        error           TEXT,
        tokens_in       INTEGER,
        tokens_out      INTEGER,
        duration_ms     INTEGER,
        created_at      TEXT NOT NULL
    )
    """,
    "CREATE INDEX delegations_by_conversation ON delegations(conversation_id, id)",
]


_SCHEMA_V14 = [
    # The memory by meaning: one vector per message or subtask report, made
    # by the local AI's embedding model (aihub/semantic.py). Deleted with
    # what it comes from (and overwritten: secure_delete).
    """
    CREATE TABLE memory_vectors (
        id              INTEGER PRIMARY KEY,
        conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        message_id      INTEGER REFERENCES messages(id) ON DELETE CASCADE,
        subtask_id      INTEGER REFERENCES subtasks(id) ON DELETE CASCADE,
        model           TEXT NOT NULL,
        vector          BLOB NOT NULL,
        created_at      TEXT NOT NULL,
        CHECK ((message_id IS NULL) <> (subtask_id IS NULL))
    )
    """,
    "CREATE UNIQUE INDEX memory_vectors_message ON memory_vectors(model, message_id) WHERE message_id IS NOT NULL",
    "CREATE UNIQUE INDEX memory_vectors_subtask ON memory_vectors(model, subtask_id) WHERE subtask_id IS NOT NULL",
    "CREATE INDEX memory_vectors_by_conversation ON memory_vectors(conversation_id, model)",
]


_SCHEMA_V15 = [
    # Several people on one server (version 0.2). The owner is the person who
    # set the server's password; the others come by invitation. Each one has
    # a password of their own (scrypt hash only).
    """
    CREATE TABLE users (
        id            INTEGER PRIMARY KEY,
        name          TEXT NOT NULL UNIQUE COLLATE NOCASE,
        password_hash TEXT NOT NULL,
        is_owner      INTEGER NOT NULL DEFAULT 0 CHECK (is_owner IN (0, 1)),
        disabled      INTEGER NOT NULL DEFAULT 0 CHECK (disabled IN (0, 1)),
        created_at    TEXT NOT NULL
    )
    """,
    "CREATE UNIQUE INDEX one_owner ON users(is_owner) WHERE is_owner = 1",
    # The server's password becomes the owner's.
    """
    INSERT INTO users (name, password_hash, is_owner, created_at)
    SELECT 'Propriétaire', password_hash, 1, changed_at FROM auth_password WHERE id = 1
    """,
    "DELETE FROM auth_password",
    # Whose session it is; the open sessions were the owner's.
    "ALTER TABLE sessions ADD COLUMN user_id INTEGER REFERENCES users(id) ON DELETE CASCADE",
    "UPDATE sessions SET user_id = (SELECT id FROM users WHERE is_owner = 1)",
    # A link the owner gives to one person; only its SHA-256 is kept, used once.
    """
    CREATE TABLE invitations (
        id         INTEGER PRIMARY KEY,
        token_hash TEXT NOT NULL UNIQUE,
        note       TEXT,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        used_at    TEXT,
        user_id    INTEGER REFERENCES users(id) ON DELETE SET NULL
    )
    """,
    # Who owns a conversation or a project: NULL is the server's owner (all
    # that existed before, and everything without accounts).
    "ALTER TABLE conversations ADD COLUMN owner_id INTEGER REFERENCES users(id) ON DELETE SET NULL",
    "ALTER TABLE projects ADD COLUMN owner_id INTEGER REFERENCES users(id) ON DELETE SET NULL",
    # A conversation or a project shared with a person, read only or read and write.
    """
    CREATE TABLE shares (
        id              INTEGER PRIMARY KEY,
        conversation_id INTEGER REFERENCES conversations(id) ON DELETE CASCADE,
        project_id      INTEGER REFERENCES projects(id) ON DELETE CASCADE,
        user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        role            TEXT NOT NULL CHECK (role IN ('read', 'write')),
        created_at      TEXT NOT NULL,
        CHECK ((conversation_id IS NULL) <> (project_id IS NULL))
    )
    """,
    "CREATE UNIQUE INDEX shares_conversation ON shares(conversation_id, user_id) WHERE conversation_id IS NOT NULL",
    "CREATE UNIQUE INDEX shares_project ON shares(project_id, user_id) WHERE project_id IS NOT NULL",
    "CREATE INDEX shares_by_user ON shares(user_id)",
    # Who sent a message (NULL: the owner, or before accounts existed).
    "ALTER TABLE messages ADD COLUMN user_id INTEGER REFERENCES users(id) ON DELETE SET NULL",
    # Whose quota a reading is (NULL: the owner's own AI accounts).
    "ALTER TABLE quota_observations ADD COLUMN user_id INTEGER REFERENCES users(id) ON DELETE CASCADE",
]


_SCHEMA_V16 = [
    # Roles (version 0.3): « invite » sees what is shared with them; « membre »
    # also makes conversations of their own; « admin » sees and manages
    # everything but the server's settings. The owner stays is_owner = 1.
    "ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'invite' CHECK (role IN ('invite', 'membre', 'admin'))",
    # A password is optional now: an empty hash is « no password » (the name is enough,
    # behind the server's password if there is one).
    # The server's own password, optional: asked once per device, before the accounts'.
    """
    CREATE TABLE server_lock (
        id            INTEGER PRIMARY KEY CHECK (id = 1),
        password_hash TEXT NOT NULL,
        changed_at    TEXT NOT NULL
    )
    """,
    # An invitation says what the person will be and see once they come.
    "ALTER TABLE invitations ADD COLUMN role TEXT NOT NULL DEFAULT 'invite' CHECK (role IN ('invite', 'membre', 'admin'))",
    "ALTER TABLE invitations ADD COLUMN shares_json TEXT",
]


_SCHEMA_V17 = [
    # A device's name (« Téléphone de Lou »), chosen by its person or when it was connected.
    "ALTER TABLE sessions ADD COLUMN label TEXT",
    # A one-time link (a QR code) that connects a device to an account without typing anything;
    # only its SHA-256 is kept, valid a few minutes.
    """
    CREATE TABLE device_links (
        id         INTEGER PRIMARY KEY,
        token_hash TEXT NOT NULL UNIQUE,
        user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        label      TEXT,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        used_at    TEXT
    )
    """,
]


_SCHEMA_V18 = [
    # A person's favorite requests (« fais le point du jour »), to insert in a click.
    # NULL user: the owner of a server without accounts.
    """
    CREATE TABLE favorites (
        id         INTEGER PRIMARY KEY,
        user_id    INTEGER REFERENCES users(id) ON DELETE CASCADE,
        title      TEXT NOT NULL,
        text       TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX favorites_by_user ON favorites(user_id, id)",
    # Files and images sent with a request. The file itself lives next to the base
    # (<base>-pieces/<conversation>/), and goes with its conversation when it is deleted.
    """
    CREATE TABLE attachments (
        id              INTEGER PRIMARY KEY,
        conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        message_id      INTEGER REFERENCES messages(id) ON DELETE CASCADE,
        user_id         INTEGER REFERENCES users(id) ON DELETE SET NULL,
        name            TEXT NOT NULL,
        stored          TEXT NOT NULL,
        mime            TEXT NOT NULL,
        size            INTEGER NOT NULL CHECK (size >= 0),
        created_at      TEXT NOT NULL
    )
    """,
    "CREATE INDEX attachments_by_conversation ON attachments(conversation_id, id)",
]


_SCHEMA_V19 = [
    # Instructions the AI always gets (« réponds en français, sois bref »), for a conversation
    # and for a project (the project's first, then the conversation's).
    "ALTER TABLE conversations ADD COLUMN instructions TEXT",
    "ALTER TABLE projects ADD COLUMN instructions TEXT",
    # Another AI's answer to the same request (« Comparer avec… »), read only, kept beside
    # the conversation's own reply; « Garder celle-ci » swaps them.
    """
    CREATE TABLE variants (
        id              INTEGER PRIMARY KEY,
        conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        request_id      INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
        provider        TEXT,
        model           TEXT,
        content         TEXT NOT NULL DEFAULT '',
        status          TEXT NOT NULL CHECK (status IN ('running', 'ok', 'error', 'stopped')),
        error           TEXT,
        tokens_in       INTEGER,
        tokens_out      INTEGER,
        duration_ms     INTEGER,
        created_at      TEXT NOT NULL
    )
    """,
    "CREATE INDEX variants_by_conversation ON variants(conversation_id, id)",
]


_SCHEMA_V20 = [
    """CREATE TABLE conversation_preferences (
        id INTEGER PRIMARY KEY,
        user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
        conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        pinned INTEGER NOT NULL DEFAULT 0 CHECK (pinned IN (0, 1)),
        archived INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1))
    )""",
    "CREATE UNIQUE INDEX preferences_person ON conversation_preferences(COALESCE(user_id, 0), conversation_id)",
    """CREATE TABLE work_templates (
        id INTEGER PRIMARY KEY,
        user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
        title TEXT NOT NULL,
        settings_json TEXT NOT NULL,
        prompt TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL
    )""",
    "CREATE INDEX templates_person ON work_templates(user_id, id)",
]


_SCHEMA_V21 = [
    """CREATE TABLE execution_policies (
        id INTEGER PRIMARY KEY,
        conversation_id INTEGER UNIQUE REFERENCES conversations(id) ON DELETE CASCADE,
        project_id INTEGER UNIQUE REFERENCES projects(id) ON DELETE CASCADE,
        policy_json TEXT NOT NULL,
        CHECK ((conversation_id IS NULL) != (project_id IS NULL))
    )""",
]


_SCHEMA_V22 = [
    """CREATE TABLE sandbox_principals (
        user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
        nonce TEXT NOT NULL UNIQUE
    )""",
]


_SCHEMA_V23 = [
    """CREATE TABLE sandbox_contexts (
        conversation_id INTEGER PRIMARY KEY REFERENCES conversations(id) ON DELETE CASCADE,
        nonce TEXT NOT NULL UNIQUE
    )""",
]


_SCHEMA_V24 = [
    'CREATE TABLE execution_suspensions (conversation_id INTEGER PRIMARY KEY '
    'REFERENCES conversations(id) ON DELETE CASCADE)',
]


class SchemaError(Exception):
    """The database file cannot be used by this version of the code."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def connect(path: str) -> sqlite3.Connection:
    """Open the database, apply pragmas and bring the schema up to date.

    `path` may be ":memory:" for tests.
    """
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        # Deleted content is overwritten with zeros: a conversation deleted for
        # good does not linger in free pages of the file.
        conn.execute("PRAGMA secure_delete = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        if path != ":memory:":
            conn.execute("PRAGMA journal_mode = WAL")
        migrate(conn)
    except BaseException:
        # A refused database must not stay locked by a connection nobody holds.
        conn.close()
        raise
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection):
    """Run a block atomically. Nested calls join the outer transaction."""
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise SchemaError(
            f"base en version {version}, code en version {SCHEMA_VERSION} : "
            "mettre à jour l'application avant d'ouvrir cette base"
        )
    if version == SCHEMA_VERSION:
        return
    with transaction(conn):
        if version < 1:
            for statement in _SCHEMA_V1:
                conn.execute(statement)
        if version < 2:
            for statement in _SCHEMA_V2:
                conn.execute(statement)
        if version < 3:
            for statement in _SCHEMA_V3:
                conn.execute(statement)
        if version < 4:
            for statement in _SCHEMA_V4:
                conn.execute(statement)
        if version < 5:
            for statement in _SCHEMA_V5:
                conn.execute(statement)
        if version < 6:
            for statement in _SCHEMA_V6:
                conn.execute(statement)
        if version < 7:
            for statement in _SCHEMA_V7:
                conn.execute(statement)
        if version < 8:
            for statement in _SCHEMA_V8:
                conn.execute(statement)
        if version < 9:
            for statement in _SCHEMA_V9:
                conn.execute(statement)
        if version < 10:
            for statement in _SCHEMA_V10:
                conn.execute(statement)
        if version < 11:
            for statement in _SCHEMA_V11:
                conn.execute(statement)
        if version < 12:
            for statement in _SCHEMA_V12:
                conn.execute(statement)
        if version < 13:
            for statement in _SCHEMA_V13:
                conn.execute(statement)
        if version < 14:
            for statement in _SCHEMA_V14:
                conn.execute(statement)
        if version < 15:
            for statement in _SCHEMA_V15:
                conn.execute(statement)
        if version < 16:
            for statement in _SCHEMA_V16:
                conn.execute(statement)
        if version < 17:
            for statement in _SCHEMA_V17:
                conn.execute(statement)
        if version < 18:
            for statement in _SCHEMA_V18:
                conn.execute(statement)
        if version < 19:
            for statement in _SCHEMA_V19:
                conn.execute(statement)
        if version < 20:
            for statement in _SCHEMA_V20:
                conn.execute(statement)
        if version < 21:
            for statement in _SCHEMA_V21:
                conn.execute(statement)
        if version < 22:
            for statement in _SCHEMA_V22:
                conn.execute(statement)
        if version < 23:
            for statement in _SCHEMA_V23:
                conn.execute(statement)
        if version < 24:
            for statement in _SCHEMA_V24:
                conn.execute(statement)
        # PRAGMA does not accept bound parameters; SCHEMA_VERSION is an int constant.
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
