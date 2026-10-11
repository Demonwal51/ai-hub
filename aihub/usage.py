"""Who used how much (« Consommation »), for the owner and the admins.

A reply, a subtask, another AI's opinion or a compared answer is counted for the person who
asked: the author of the request it answers (a reply: the request before it
in its conversation; a subtask: its plan's request; an opinion: the last
request of its conversation at that time). No author (the owner, or before
accounts existed): the owner. Imported replies (no provider) cost nothing
here and are left out. Tokens are what the AIs reported; some report none.
"""

from datetime import datetime, timedelta, timezone

PERIODS = (1, 7, 30, 365)

# The author of a request: the user message before `id` in `conversation_id` (at `created_at` for an opinion).
_REPLIES = """
SELECT (SELECT u.user_id FROM messages u WHERE u.conversation_id = a.conversation_id AND u.id < a.id
        AND u.role = 'user' ORDER BY u.id DESC LIMIT 1) AS user_id,
       a.provider, COUNT(*) AS runs, COALESCE(SUM(a.tokens_in), 0) AS tokens_in,
       COALESCE(SUM(a.tokens_out), 0) AS tokens_out
FROM messages a
WHERE a.role = 'assistant' AND a.provider IS NOT NULL AND a.created_at >= ?
GROUP BY 1, 2
"""
_SUBTASKS = """
SELECT r.user_id, s.provider, COUNT(*) AS runs, COALESCE(SUM(s.tokens_in), 0) AS tokens_in,
       COALESCE(SUM(s.tokens_out), 0) AS tokens_out
FROM subtasks s JOIN plans p ON p.id = s.plan_id LEFT JOIN messages r ON r.id = p.request_message_id
WHERE s.provider IS NOT NULL AND s.started_at IS NOT NULL AND s.started_at >= ?
GROUP BY 1, 2
"""
_OPINIONS = """
SELECT (SELECT u.user_id FROM messages u WHERE u.conversation_id = d.conversation_id AND u.role = 'user'
        AND u.created_at <= d.created_at ORDER BY u.id DESC LIMIT 1) AS user_id,
       d.provider, COUNT(*) AS runs, COALESCE(SUM(d.tokens_in), 0) AS tokens_in,
       COALESCE(SUM(d.tokens_out), 0) AS tokens_out
FROM delegations d WHERE d.created_at >= ?
GROUP BY 1, 2
"""


_VARIANTS = """
SELECT r.user_id, v.provider, COUNT(*) AS runs, COALESCE(SUM(v.tokens_in), 0) AS tokens_in,
       COALESCE(SUM(v.tokens_out), 0) AS tokens_out
FROM variants v LEFT JOIN messages r ON r.id = v.request_id
WHERE v.provider IS NOT NULL AND v.created_at >= ?
GROUP BY 1, 2
"""


def since(days, now=None):
    now = now or datetime.now(timezone.utc)
    return (now - timedelta(days=days)).isoformat(timespec="milliseconds")


def usage(conn, days=7, now=None):
    """[{user_id, name, runs, tokens_in, tokens_out, by_provider: {provider: {...}}, requests}], the most first."""
    if days not in PERIODS:
        raise ValueError(f"période : {', '.join(map(str, PERIODS))} jours")
    start = since(days, now)
    owner = conn.execute("SELECT id, name FROM users WHERE is_owner = 1").fetchone()
    names = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM users")}
    people = {}

    def person(user_id):
        key = user_id if user_id in names else (owner["id"] if owner else None)
        if key not in people:
            people[key] = {"user_id": key, "name": names.get(key, "Propriétaire"), "requests": 0, "runs": 0,
                           "tokens_in": 0, "tokens_out": 0, "by_provider": {}}
        return people[key]

    for query in (_REPLIES, _SUBTASKS, _OPINIONS, _VARIANTS):
        for row in conn.execute(query, (start,)):
            p = person(row["user_id"])
            for field in ("runs", "tokens_in", "tokens_out"):
                p[field] += row[field]
                slot = p["by_provider"].setdefault(row["provider"], {"runs": 0, "tokens_in": 0, "tokens_out": 0})
                slot[field] += row[field]
    # A request: a message an AI answered (an imported one, never sent, is not one).
    for row in conn.execute(
            "SELECT u.user_id, COUNT(*) AS n FROM messages u WHERE u.role = 'user' AND u.created_at >= ? AND EXISTS "
            "(SELECT 1 FROM messages a WHERE a.conversation_id = u.conversation_id AND a.id > u.id "
            "AND a.role = 'assistant' AND a.provider IS NOT NULL AND NOT EXISTS (SELECT 1 FROM messages b "
            "WHERE b.conversation_id = u.conversation_id AND b.id > u.id AND b.id < a.id AND b.role = 'user')) "
            "GROUP BY 1", (start,)):
        person(row["user_id"])["requests"] += row["n"]
    return sorted(people.values(), key=lambda p: (p["tokens_in"] + p["tokens_out"], p["runs"]), reverse=True)
