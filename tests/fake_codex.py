"""A fake Codex CLI for the tests: `login status` and the app server's JSON-RPC.

Its messages copy those of the real codex-cli 0.144.2 (measured with
sonde_app_server.py). Driven by environment variables:
- FAKE_CODEX_SCENARIO: hello (default), commentary, command, patch, quota,
  failed, slow, schema, requests, newer;
- FAKE_CODEX_LOG: a JSON file where it writes what it received;
- FAKE_CODEX_LOGGED_OUT=1: `login status` says not logged in;
- FAKE_CODEX_LOST=1: thread/resume finds no rollout;
- FAKE_CODEX_MODELS: models of model/list, comma separated.
"""

import json
import os
import sys
import time
import uuid

SCENARIO = os.environ.get("FAKE_CODEX_SCENARIO", "hello")
LOG = os.environ.get("FAKE_CODEX_LOG")
state = {"args": sys.argv[1:], "pid": os.getpid(), "answers": [], "requests": []}
_next = [100]


def save():
    if LOG:
        with open(LOG, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False)


def send(message):
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def notify(method, params):
    send({"method": method, "params": params})


def read():
    line = sys.stdin.readline()
    if not line:
        save()
        sys.exit(0)
    return json.loads(line)


def ask(method, params):
    """A request of the server; returns the client's answer (answers to other ids are not expected)."""
    _next[0] += 1
    send({"id": _next[0], "method": method, "params": params})
    while True:
        message = read()
        if message.get("id") == _next[0] and "method" not in message:
            state["answers"].append({"method": method, **{k: message[k] for k in ("result", "error") if k in message}})
            save()
            return message


def limits(percent=12, reached=None):
    return {"limitId": "codex", "primary": {"usedPercent": percent, "windowDurationMins": 300, "resetsAt": 1791653530},
            "secondary": {"usedPercent": 3, "windowDurationMins": 10080, "resetsAt": 1792240330},
            "planType": "plus", "rateLimitReachedType": reached}


def agent_message(thread, turn, text, phase="final_answer", parts=None):
    item_id = "msg_" + uuid.uuid4().hex[:8]
    notify("item/started", {"item": {"type": "agentMessage", "id": item_id, "text": "", "phase": phase},
                            "threadId": thread, "turnId": turn})
    for part in parts or [text]:
        notify("item/agentMessage/delta", {"delta": part, "itemId": item_id, "threadId": thread, "turnId": turn})
    notify("item/completed", {"item": {"type": "agentMessage", "id": item_id, "text": text, "phase": phase},
                              "threadId": thread, "turnId": turn})


def usage(thread, turn, given, out):
    breakdown = {"totalTokens": given + out, "inputTokens": given, "cachedInputTokens": given // 2,
                 "outputTokens": out, "reasoningOutputTokens": 0}
    notify("thread/tokenUsage/updated", {"threadId": thread, "turnId": turn,
                                         "tokenUsage": {"total": breakdown, "last": breakdown}})


def complete(thread, turn, status="completed", error=None):
    notify("turn/completed", {"threadId": thread, "turn": {"id": turn, "items": [], "status": status,
                                                           "error": error, "durationMs": 5}})


def _pick(prompt):
    """FAKE_CODEX_SCENARIO=auto (the trial server): the scenario follows the request."""
    low = prompt.lower()
    for word, scenario in (("écris", "patch"), ("commande", "command"), ("quota", "quota"),
                           ("attends", "slow"), ("pense", "commentary")):
        if word in low:
            return scenario
    return "hello"


def turn(thread, turn_id, prompt):
    s = SCENARIO if SCENARIO != "auto" else _pick(prompt)
    if s =="slow":
        notify("turn/started", {"threadId": thread, "turn": {"id": turn_id, "status": "inProgress"}})
        time.sleep(60)
    if s =="commentary":
        agent_message(thread, turn_id, "Je regarde.", phase="commentary")
        usage(thread, turn_id, 1000, 10)
    if s =="command":
        item = {"type": "commandExecution", "id": "exec-1", "status": "inProgress",
                "command": '"powershell.exe" -Command "Set-Content x.txt bonjour"',
                "commandActions": [{"type": "unknown", "command": "Set-Content x.txt bonjour"}]}
        notify("item/started", {"item": item, "threadId": thread, "turnId": turn_id})
        answer = ask("item/commandExecution/requestApproval", {
            "threadId": thread, "turnId": turn_id, "itemId": "exec-1", "startedAtMs": 1,
            "reason": "Créer x.txt ?", "command": item["command"], "commandActions": item["commandActions"]})
        accepted = (answer.get("result") or {}).get("decision") == "accept"
        item["status"] = "completed" if accepted else "declined"
        notify("item/completed", {"item": item, "threadId": thread, "turnId": turn_id})
        agent_message(thread, turn_id, "commande lancée" if accepted else "commande refusée")
    elif s =="patch":
        changes = [{"path": "C:\\w\\a.txt", "kind": {"type": "add"}, "diff": "bonjour\n"},
                   {"path": "C:\\w\\b.txt", "kind": {"type": "update"}, "diff": "-x\n+y\n"}]
        notify("item/started", {"item": {"type": "fileChange", "id": "patch-1", "changes": changes,
                                         "status": "inProgress"}, "threadId": thread, "turnId": turn_id})
        answer = ask("item/fileChange/requestApproval", {"threadId": thread, "turnId": turn_id,
                                                         "itemId": "patch-1", "startedAtMs": 1, "reason": None})
        accepted = (answer.get("result") or {}).get("decision") == "accept"
        notify("item/completed", {"item": {"type": "fileChange", "id": "patch-1", "changes": changes,
                                           "status": "completed" if accepted else "declined"},
                                  "threadId": thread, "turnId": turn_id})
        agent_message(thread, turn_id, "fichiers écrits" if accepted else "fichiers refusés")
    elif s =="requests":
        for method, params in (
                ("account/chatgptAuthTokens/refresh", {"reason": "expired"}),
                ("item/permissions/requestApproval", {"threadId": thread, "turnId": turn_id, "itemId": "p"}),
                ("mcpServer/elicitation/request", {"threadId": thread, "serverName": "aihub", "mode": "form",
                                                   "message": "Autoriser ?", "requestedSchema": {}}),
                ("mcpServer/elicitation/request", {"threadId": thread, "serverName": "autre", "mode": "form",
                                                   "message": "Autoriser ?", "requestedSchema": {}}),
                ("item/tool/requestUserInput", {"threadId": thread})):
            ask(method, params)
        notify("item/completed", {"item": {"type": "mcpToolCall", "id": "m1", "server": "aihub",
                                           "tool": "search_project", "status": "completed"},
                                  "threadId": thread, "turnId": turn_id})
        agent_message(thread, turn_id, "fini")
    elif s =="quota":
        notify("account/rateLimits/updated", {"rateLimits": limits(100, "primary")})
        error = {"message": json.dumps({"type": "error", "status": 429, "error": {
            "type": "usage_limit_reached", "message": "You've hit your usage limit."}}),
            "codexErrorInfo": "usageLimitExceeded"}
        notify("error", {"error": error, "threadId": thread, "turnId": turn_id, "willRetry": False})
        complete(thread, turn_id, "failed", error)
        return
    elif s =="newer":
        error = {"message": json.dumps({"type": "error", "status": 400, "error": {
            "type": "invalid_request_error",
            "message": "The 'gpt-6-astra' model requires a newer version of Codex. Please upgrade."}}),
            "codexErrorInfo": "badRequest"}
        complete(thread, turn_id, "failed", error)
        return
    elif s =="failed":
        complete(thread, turn_id, "failed", {"message": "boom", "codexErrorInfo": "other"})
        return
    elif s =="schema":
        agent_message(thread, turn_id, json.dumps({"summary": "plan", "subtasks": []}))
    elif s in ("hello", "commentary"):
        agent_message(thread, turn_id, "Bonjour", parts=["Bon", "jour"])
    usage(thread, turn_id, 12000, 5)
    notify("account/rateLimits/updated", {"rateLimits": limits()})
    complete(thread, turn_id)


def app_server():
    thread = None
    while True:
        message = read()
        method, rid, params = message.get("method"), message.get("id"), message.get("params") or {}
        if rid is None:
            state.setdefault("notifications", []).append(method)
            continue
        state["requests"].append(method)
        if method == "initialize":
            send({"id": rid, "result": {"userAgent": "fake", "platformOs": "windows"}})
        elif method == "account/rateLimits/read":
            send({"id": rid, "result": {"rateLimits": limits(42)}})
        elif method == "model/list" and "params" not in message:
            send({"id": rid, "error": {"code": -32600, "message": "Invalid request: missing field `params`"}})
        elif method == "model/list":
            models = os.environ.get("FAKE_CODEX_MODELS", "gpt-5.6-sol,gpt-5.6-luna").split(",")
            send({"id": rid, "result": {"data": [{"id": m, "model": m} for m in models if m]}})
        elif method in ("thread/start", "thread/resume"):
            state["thread_method"], state["thread_params"] = method, params
            if method == "thread/resume" and os.environ.get("FAKE_CODEX_LOST"):
                send({"id": rid, "error": {"code": -32600,
                                           "message": f"no rollout found for thread id {params.get('threadId')}"}})
                save()
                continue
            thread = params.get("threadId") or str(uuid.uuid4())
            send({"id": rid, "result": {"thread": {"id": thread}, "model": params.get("model") or "gpt-5.6-sol",
                                        "approvalPolicy": params.get("approvalPolicy")}})
            notify("thread/started", {"thread": {"id": thread}})
        elif method == "turn/start":
            text = "".join(part.get("text", "") for part in params.get("input") or [])
            state["prompt"], state["turn_params"] = text, params
            save()
            turn_id = "turn-" + uuid.uuid4().hex[:6]
            send({"id": rid, "result": {"turn": {"id": turn_id, "status": "inProgress"}}})
            turn(thread, turn_id, text)
        else:
            send({"id": rid, "error": {"code": -32601, "message": f"unknown method {method}"}})
        save()


def main():
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")  # like the real CLI, whatever the console's code page
    if sys.argv[1:3] == ["login", "status"]:
        if os.environ.get("FAKE_CODEX_LOGGED_OUT"):
            print("Not logged in")
            return 1
        print("Logged in using ChatGPT")
        return 0
    if sys.argv[1:2] == ["app-server"]:
        save()
        app_server()
    print(f"commande inconnue : {sys.argv[1:]}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
