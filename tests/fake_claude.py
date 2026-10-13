"""Stand-in for the Claude Code CLI, speaking the same stream-json protocol.

The scenario comes from FAKE_CLAUDE_SCENARIO. Arguments, the received user
message and permission answers are written to FAKE_CLAUDE_LOG (JSON) so tests
can check what the provider sent.
"""

import json
import os
import subprocess
import sys
import time

SESSION = "11111111-2222-3333-4444-555555555555"
LOG = os.environ.get("FAKE_CLAUDE_LOG")
SCENARIO = os.environ.get("FAKE_CLAUDE_SCENARIO", "text")

# With FAKE_CLAUDE_SCENARIO=auto (manual trials of the interface), the
# scenario is picked from a word of the prompt.
AUTO_WORDS = (("plan_test", "exit_plan_wait"), ("écris", "permission"), ("question", "question"), ("attends", "hang"),
              ("connexion", "not_logged_in"), ("quota", "quota"), ("plante", "crash"),
              ("lent", "slow"), ("consulte", "consult"))

# The conductor's default plan: two independent subtasks, then one that needs both.
DEFAULT_PLAN = {
    "summary": "Deux parties en parallèle, puis l'assemblage.",
    "max_parallel": 2,
    "subtasks": [
        {"title": "Partie A", "description": "faire la partie A", "kind": "code",
         "depends_on": [], "files": ["a.txt"]},
        {"title": "Partie B", "description": "faire la partie B", "kind": "redaction",
         "depends_on": [], "files": ["b.txt"]},
        {"title": "Assemblage", "description": "assembler A et B", "kind": "terminal",
         "depends_on": [1, 2], "files": ["c.txt"]},
    ],
}
LOG_DIR = os.environ.get("FAKE_CLAUDE_LOG_DIR")  # one log per process, for parallel runs
NO_WINDOW = 0x08000000 if os.name == "nt" else 0

log = {"args": sys.argv[1:], "cwd": os.getcwd(), "answers": []}


def _write(path):
    # Whole or not at all: a test reading it meanwhile never sees it empty (written aside, then swapped).
    aside = f"{path}.{os.getpid()}.tmp"
    with open(aside, "w", encoding="utf-8") as fh:
        json.dump(log, fh)
    for _ in range(50):
        try:
            os.replace(aside, path)
            return
        except PermissionError:  # Windows: the test has it open for reading this instant
            time.sleep(0.01)
    os.replace(aside, path)


def save():
    if LOG:
        _write(LOG)
    if LOG_DIR:
        _write(os.path.join(LOG_DIR, f"run-{os.getpid()}.json"))


def option(name):
    args = sys.argv[1:]
    return args[args.index(name) + 1] if name in args and args.index(name) + 1 < len(args) else None


def conductor_answer():
    """Planning or adapting: answer with the plan, as structured output."""
    schema = json.loads(option("--json-schema"))
    plan = json.loads(os.environ["FAKE_PLAN"]) if os.environ.get("FAKE_PLAN") else json.loads(json.dumps(DEFAULT_PLAN))
    agent_enum = (schema["properties"]["subtasks"]["items"]["properties"].get("agent") or {}).get("enum")
    if agent_enum:  # adapting: every subtask gets the first agent offered
        plan["summary"] = "Plan adapté aux agents disponibles."
        for item in plan["subtasks"]:
            # FAKE_ADAPT_AGENT names an agent instead (an unknown one leaves gaps).
            item["agent"] = os.environ.get("FAKE_ADAPT_AGENT") or agent_enum[0]
    log["structured"] = plan
    save()
    delta("Je prépare le plan.")
    emit({"type": "result", "subtype": "success", "is_error": False, "result": json.dumps(plan),
          "structured_output": plan, "session_id": SESSION,
          "usage": {"input_tokens": 5, "cache_read_input_tokens": 100, "output_tokens": 50}})


def emit(event):
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()


def delta(text):
    emit({"type": "stream_event", "event": {
        "type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}}})


def result(text, *, is_error=False, api_error_status=None, usage=None, num_turns=1):
    emit({"type": "result", "subtype": "success", "is_error": is_error, "result": text,
          "session_id": SESSION, "api_error_status": api_error_status, "num_turns": num_turns,
          "usage": usage or {"input_tokens": 12, "cache_creation_input_tokens": 100,
                             "cache_read_input_tokens": 30, "output_tokens": 7},
          "permission_denials": []})


def prompts_reach_host():
    """Like the real CLI: prompts go to the host only with --permission-prompt-tool stdio."""
    args = sys.argv[1:]
    return any(a == "--permission-prompt-tool" and args[i + 1:i + 2] == ["stdio"]
               for i, a in enumerate(args))


def ask(tool, tool_input, request_id):
    if not prompts_reach_host():
        # The real CLI then denies the tool itself, without asking anyone.
        return {"behavior": "deny", "message": "you haven't granted it yet"}
    emit({"type": "control_request", "request_id": request_id, "request": {
        "subtype": "can_use_tool", "tool_name": tool, "input": tool_input,
        "permission_suggestions": []}})
    while True:
        line = sys.stdin.readline()
        if not line:
            return None
        message = json.loads(line)
        if message.get("type") == "control_response":
            answer = message["response"]
            log["answers"].append(answer)
            save()
            return answer["response"]


def consult(prompt):
    """Use the hub's MCP server like the real CLI: start it, list its tools, call one.

    "consulte liste", "consulte cherche <mots>", "consulte lis <numéro> [depuis]".
    """
    raw = option("--mcp-config")
    if raw is None:
        return "aucun accès aux autres conversations"
    server = json.loads(raw)["mcpServers"]["aihub"]
    allowed = (option("--allowedTools") or "").split(",")
    proc = subprocess.Popen([server["command"], *server["args"]], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, text=True, encoding="utf-8", creationflags=NO_WINDOW)

    def rpc(mid, method, params=None):
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}}) + "\n")
        proc.stdin.flush()
        return json.loads(proc.stdout.readline())

    try:
        rpc(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        tools = [t["name"] for t in rpc(2, "tools/list")["result"]["tools"]]
        log["mcp_tools"] = tools
        words = prompt.split("consulte", 1)[1].split()
        if words[:1] == ["cherche"]:
            name, arguments = "search_project", {"query": " ".join(words[1:])}
        elif words[:1] == ["lis"]:
            name, arguments = "read_conversation", {"conversation_id": int(words[1])}
            if len(words) > 2:
                arguments["start"] = int(words[2])
        else:
            name, arguments = "list_project_conversations", {}
        # The CLI asks the host unless the tool is in --allowedTools.
        full = f"mcp__aihub__{name}"
        emit({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "toolu_1", "name": full, "input": arguments}]}})
        if full not in allowed:
            answer = ask(full, arguments, "req-mcp")
            if not answer or answer.get("behavior") != "allow":
                return "outil refusé"
        reply = rpc(3, "tools/call", {"name": name, "arguments": arguments})["result"]
        log["mcp_call"] = {"name": name, "arguments": arguments, "is_error": reply["isError"]}
        save()
        return reply["content"][0]["text"]
    finally:
        proc.stdin.close()
        proc.wait(timeout=10)


def answer_usage(request_id):
    """Reply to get_usage like the real CLI (shape measured on 2.1.293); FAKE_USAGE picks the case."""
    case = os.environ.get("FAKE_USAGE", "ok")
    previous = 0
    if LOG and os.path.exists(LOG):  # counted across runs, so tests can see a cache work
        try:
            with open(LOG, encoding="utf-8") as fh:
                previous = json.load(fh).get("usage_calls", 0)
        except ValueError:
            pass
    log["usage_calls"] = previous + 1
    save()
    if case == "error":
        emit({"type": "control_response", "response": {
            "subtype": "error", "request_id": request_id, "error": "Not logged in"}})
    elif case == "unavailable":
        emit({"type": "control_response", "response": {
            "subtype": "success", "request_id": request_id,
            "response": {"rate_limits_available": False, "rate_limits": None}}})
    else:
        five = {"high": 92, "mid": 72}.get(case, 29)
        emit({"type": "control_response", "response": {
            "subtype": "success", "request_id": request_id, "response": {
                "subscription_type": "pro", "rate_limits_available": True,
                "rate_limits": {
                    "five_hour": {"utilization": five, "resets_at": "2026-10-09T17:39:59.717875+00:00"},
                    "seven_day": {"utilization": 13, "resets_at": "2026-10-15T22:59:59.717895+00:00"},
                    "seven_day_opus": None,
                    "limits": [{"kind": "session", "percent": five, "severity": "normal"},
                               {"kind": "weekly_all", "percent": 13, "severity": "normal"}],
                }}}})
    sys.stdin.read()


def main():
    # Like the real CLI, speak UTF-8 whatever the Windows code page or
    # PYTHONUTF8 say: without this, "é" arrives as "Ã©".
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")
    # A person's own account (CLAUDE_CONFIG_DIR): signed in once a sign-in left its mark there.
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    mark = os.path.join(config_dir, "fake-account.txt") if config_dir else None
    if sys.argv[1:3] == ["auth", "status"]:
        logged_in = os.path.exists(mark) if mark else os.environ.get("FAKE_CLAUDE_LOGGED_IN") == "1"
        print(json.dumps({"loggedIn": logged_in,
                          "authMethod": "claude.ai" if logged_in else "none"}, indent=2))
        sys.exit(0 if logged_in else 1)
    if sys.argv[1:3] == ["auth", "login"]:
        # Like the real CLI (measured on 2.1.293): a page, then a prompt without an end of line.
        log["browser"] = os.environ.get("BROWSER")
        save()
        print("Opening browser to sign in…")
        print("If the browser didn't open, visit: https://example.test/oauth/authorize?code=true&state=abc")
        print("Paste code here if prompted > ", end="", flush=True)
        code = sys.stdin.readline().strip()
        if code == "bon-code" and mark:
            with open(mark, "w", encoding="utf-8") as fh:
                fh.write("signed in")
            print("\nLogin successful.")
            sys.exit(0)
        print("\nOAuth error: Invalid code")
        sys.exit(1)
    if sys.argv[1:3] == ["auth", "logout"]:
        if mark and os.path.exists(mark):
            os.remove(mark)
        print("Successfully logged out.")
        sys.exit(0)
    global SCENARIO
    log["config_dir"] = config_dir  # whose account runs the request
    if mark and not os.path.exists(mark):
        SCENARIO = "not_logged_in"
    first = sys.stdin.readline()
    message = json.loads(first) if first.strip() else None
    if message and message.get("type") == "control_request" \
            and message.get("request", {}).get("subtype") == "get_usage":
        log["usage_config_dir"] = config_dir  # whose quota is read
        save()
        answer_usage(message["request_id"])
        return
    log["user_message"] = message
    log["mcp_tool_timeout"] = os.environ.get("MCP_TOOL_TIMEOUT")  # what the hub sets for a delegation
    log["started"] = time.time()
    save()
    prompt = ((message or {}).get("message") or {}).get("content", "")
    # Passages the hub joins from other conversations are context, not the
    # user's words: they must not pick the scenario.
    if "<extraits_du_projet>" in prompt:
        log["joined"] = prompt[prompt.index("<extraits_du_projet>"):]
        prompt = prompt[:prompt.index("<extraits_du_projet>")].rstrip()
        save()
    if prompt.startswith("<memoire_de_la_conversation>") and "\n\nDemande :\n" in prompt:
        # A fresh start: the memory first, then the user's own request.
        log["memory"], prompt = prompt.split("\n\nDemande :\n", 1)
        save()
    if option("--resume") and os.environ.get("FAKE_LOST_SESSION") == "1":
        # Like the real CLI when it no longer has the session (measured on
        # 2.1.293): an empty result, the reason in "errors", and on stderr.
        log["lost_session"] = option("--resume")
        save()
        reason = f"No conversation found with session ID: {option('--resume')}"
        emit({"type": "result", "subtype": "error_during_execution", "is_error": True, "num_turns": 0,
              "session_id": option("--resume"), "errors": [reason], "usage": {}})
        sys.stderr.write(reason + "\n")
        sys.exit(1)
    if prompt.startswith("Résume cette conversation") or prompt.startswith("Raccourcis ce résumé"):
        SCENARIO = "summary"
    if SCENARIO == "auto":
        if option("--json-schema"):
            SCENARIO = "conductor"
        elif "Écris la synthèse finale" in prompt:
            SCENARIO = "synthesis"
        elif prompt.startswith("Discussion avec l'utilisateur."):
            SCENARIO = "discussion"
        elif "<mode_autonome>" in prompt:
            SCENARIO = "autonome"
        elif "Ta sous-tâche" in prompt:
            # Only the subtask's own text decides: the user's request, quoted
            # for context, would otherwise trigger the same case everywhere.
            part = prompt.split("Ta sous-tâche", 1)[1]
            part = part.split("\n\nFichiers prévus")[0].split("\n\nRésultat de la sous-tâche")[0]
            log["subtask"] = part.strip()
            SCENARIO = next((s for word, s in AUTO_WORDS if word in part.lower()), "subtask")
        else:
            SCENARIO = next((s for word, s in AUTO_WORDS if word in prompt.lower()), "text")
        log["scenario"] = SCENARIO
        save()
    emit({"type": "system", "subtype": "init", "session_id": SESSION,
          "model": "claude-test-model", "permissionMode": "default"})

    if SCENARIO == "text":
        emit("not an object")
        sys.stdout.write("pas du json\n")
        delta("Bon")
        delta("jour")
        emit({"type": "rate_limit_event", "rate_limit_info": {
            "status": "allowed_warning", "rateLimitType": "five_hour", "utilization": 0.82,
            "resetsAt": 1791000000,
            "unifiedWindows": {"five_hour": {"utilization": 0.82, "resetsAt": 1791000000}}}})
        emit({"type": "assistant", "message": {"content": [{"type": "text", "text": "Bonjour"}]}})
        result("Bonjour")

    elif SCENARIO == "permission":
        answer = ask("Write", {"file_path": "note.txt", "content": "contenu"}, "req-1")
        if answer and answer.get("behavior") == "allow":
            data = answer.get("updatedInput") or {}
            with open(data.get("file_path", "note.txt"), "w", encoding="utf-8") as fh:
                fh.write(data.get("content", ""))
            delta("écrit")
            result("écrit")
        else:
            message = (answer or {}).get("message", "")
            # A pause after a refusal: tests can tell "freed at the refusal"
            # from "freed when this agent ends".
            time.sleep(1)
            delta("refusé")
            result(f"refusé : {message}")

    elif SCENARIO == "exit_plan_wait":
        answer = ask('ExitPlanMode', {'plan':'Plan de test à valider.'}, 'plan-request')
        if answer and answer.get('behavior') == 'allow':
            result('Plan validé')
        else:
            # A streaming CLI may wait for another user turn after a denial.
            # AI Hub cannot send that turn while this request is still busy.
            sys.stdin.read()
            result('Plan refusé')

    elif SCENARIO == 'exit_plan_buffered':
        for name, request_id in (('ExitPlanMode','plan-request'), ('Write','buffered-write')):
            emit({'type':'control_request','request_id':request_id,
                  'request':{'subtype':'can_use_tool','tool_name':name,'input':{'plan':'Plan de test'}}})
        sys.stdin.read()

    elif SCENARIO == "conductor":
        conductor_answer()

    elif SCENARIO == "summary":
        # A summary that can be checked: the requests it saw, or a shortened one.
        log["summary_prompt"] = prompt
        log["summary_tools"] = option("--tools")
        save()
        if os.environ.get("FAKE_SUMMARY_FAIL") == "1":
            result("Claude usage limit reached", is_error=True, api_error_status=429)
            sys.stdin.read()
            return
        if prompt.startswith("Raccourcis"):
            words = int(prompt.split("à ", 1)[1].split(" ", 1)[0])
            text = " ".join(prompt.split("\n\n", 1)[1].split()[:words])
        else:
            asked = [line[len("[Utilisateur] "):][:80] for line in prompt.splitlines() if line.startswith("[Utilisateur] ")]
            text = "Résumé : " + " / ".join(asked) + " " + os.environ.get("FAKE_SUMMARY_PAD", "")
        delta(text)
        result(text.strip())

    elif SCENARIO == "consult":
        text = consult(prompt)
        delta(text)
        result(text, num_turns=2 if "mcp_call" in log else 1)

    elif SCENARIO == "autonome":
        # A round of autonomous work: says « fini » from round FAKE_AUTONOMOUS_DONE_AT (2 by default).
        import re
        found = re.search(r"Tour (\d+) sur", prompt)
        number = int(found.group(1)) if found else 1
        log["round"] = number
        save()
        if os.environ.get("FAKE_AUTONOMOUS_FAIL_AT") == str(number):
            result("Claude usage limit reached", is_error=True, api_error_status=429)
            sys.stdin.read()
            return
        if os.environ.get("FAKE_AUTONOMOUS_SLOW"):
            time.sleep(float(os.environ["FAKE_AUTONOMOUS_SLOW"]))
        with open(f"tour-{number}.txt", "w", encoding="utf-8") as fh:  # the work the check will look at
            fh.write("fait")
        done_at = int(os.environ.get("FAKE_AUTONOMOUS_DONE_AT", "2"))
        state = "" if os.environ.get("FAKE_AUTONOMOUS_SILENT") else \
            f"\nÉTAT : {'FINI' if number >= done_at else 'À SUIVRE'}"
        text = f"tour {number} fait.{state}"
        delta(text)
        result(text)

    elif SCENARIO == "discussion":
        said = prompt.split("<message>\n", 1)[-1].split("\n</message>", 1)[0]
        text = f"Le chef répond à : {said}" + (" (plan vu)" if "Plan en cours" in prompt else "")
        delta(text)
        result(text)

    elif SCENARIO == "synthesis":
        ok, failed = prompt.count("— réussie"), prompt.count("— ÉCHEC")
        text = f"Synthèse : {ok} réussie(s), {failed} en échec."
        delta(text)
        result(text)

    elif SCENARIO in ("subtask", "slow"):
        if SCENARIO == "slow":
            time.sleep(1.5)
        title = log["subtask"].splitlines()[0].split(":", 1)[-1].strip()
        delta("fait : ")
        delta(title)
        result(f"fait : {title}")

    elif SCENARIO == "question":
        answer = ask("AskUserQuestion", {"questions": [{
            "question": "Quelle couleur ?", "header": "Couleur", "multiSelect": False,
            "options": [{"label": "Rouge", "description": "chaud"}, {"label": "Bleu"}]}]}, "req-q")
        if answer and answer.get("behavior") == "allow":
            answers = (answer.get("updatedInput") or {}).get("answers", {})
            delta("réponse reçue")
            result(f"réponse : {answers.get('Quelle couleur ?')}")
        else:
            result(f"question refusée : {(answer or {}).get('message', '')}")

    elif SCENARIO == "unknown_control":
        emit({"type": "control_request", "request_id": "req-x", "request": {"subtype": "hook_callback"}})
        line = sys.stdin.readline()
        log["answers"].append(json.loads(line)["response"])
        save()
        result("fini")

    elif SCENARIO == "not_logged_in":
        emit({"type": "assistant", "error": "authentication_failed",
              "message": {"content": [{"type": "text", "text": "Not logged in · Please run /login"}]}})
        result("Not logged in · Please run /login", is_error=True,
               usage={"input_tokens": 0, "output_tokens": 0})

    elif SCENARIO == "quota":
        emit({"type": "rate_limit_event", "rate_limit_info": {
            "status": "rejected", "rateLimitType": "five_hour", "resetsAt": 1791000000}})
        result("Claude usage limit reached", is_error=True, api_error_status=429)

    elif SCENARIO == "hang":
        # Bounded, so a provider that fails to stop it cannot leave it running for ever.
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(45)"],
                                 creationflags=NO_WINDOW)
        log["child_pid"] = child.pid
        log["pid"] = os.getpid()
        save()
        delta("je travaille")
        time.sleep(45)
        return

    elif SCENARIO == "crash":
        sys.stderr.write("boom: erreur interne\n")
        sys.stderr.flush()
        sys.exit(3)

    else:
        raise SystemExit(f"scénario inconnu : {SCENARIO}")

    # Like the real CLI, wait for stdin to close before exiting.
    log["ended"] = time.time()
    save()
    sys.stdin.read()


if __name__ == "__main__":
    main()
