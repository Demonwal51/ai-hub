"""AI providers: each one runs an agent and reports text, permissions and quota.

Claude Code and Codex share one interface (a run with `run()` and `stop()`,
returning a RunResult). This module picks the right one for an agent.
"""

from pathlib import Path, PurePosixPath

from . import claude_code, codex, ollama
from .claude_code import ProviderError

MODULES = {claude_code.PROVIDER: claude_code, codex.PROVIDER: codex, ollama.PROVIDER: ollama}
NAMES = {claude_code.PROVIDER: claude_code.DISPLAY_NAME, codex.PROVIDER: codex.DISPLAY_NAME,
         "ollama": "l'IA locale"}


def module(provider):
    try:
        return MODULES[provider]
    except KeyError:
        raise ProviderError(f"le fournisseur « {NAMES.get(provider, provider)} » n'est pas encore branché") from None


def command_for(commands, provider, *, execution=None):
    """The command that starts this provider's CLI.

    `commands` is what the hub was given: None (each CLI is searched for),
    a list (the Claude Code command, as before Codex), or a dict by
    provider, where False means "not offered here".
    """
    if isinstance(commands, dict):
        given = commands.get(provider)
    elif commands is not None and provider == claude_code.PROVIDER:
        given = commands
    else:
        given = None
    if given is False:
        raise ProviderError(f"{NAMES.get(provider, provider)} n'est pas branché sur ce serveur")
    if execution is not None:
        if provider == 'ollama':
            return list(given) if given else ollama.find_cli()
        if provider not in ('claude_code', 'codex'):
            raise ProviderError('ce fournisseur ne prend pas en charge les espaces isolés')
        return ['claude' if provider == 'claude_code' else 'codex']
    return list(given) if given else module(provider).find_cli()


def default_model(provider, model):
    """None when the agent's model means "the CLI's own default" (no model sent)."""
    return None if model in (None, module(provider).DEFAULT_MODEL) else model


def new_run(provider, command, *, instructions=None, warning_from=None, web=True, attachments=(), execution=None, **options):
    """A run of this provider. `instructions`: text added to the agent's system prompt;
    `web`: the AI's own web search (off: taken away). `attachments`: files sent with the
    request, [{"path", "image"}] (their names and paths are in the prompt already): Claude
    Code may read their folder, Codex and the local AI get the images themselves."""
    if execution is not None:
        if provider == 'ollama':
            from urllib.parse import urlsplit
            target = urlsplit(command[0])
            if target.hostname not in ('localhost', '127.0.0.1', '::1') or target.scheme not in ('http', 'https'):
                raise ProviderError('un espace local isolé utilise uniquement Ollama sur cet ordinateur')
        execution.reject_host_tools(options)
        execution.prepare(provider)
        attachments, mapped = execution.copy_inputs(attachments)
        for source, target in mapped.items():
            options['prompt'] = options['prompt'].replace(source, target)
        if provider != 'ollama':
            command = ['claude' if provider == 'claude_code' else 'codex']
        options['env'] = None
        options['execution'] = execution
        web = False
    images = [a["path"] for a in attachments if a.get("image")]
    if provider == codex.PROVIDER:
        extra = {"warning_from": warning_from} if warning_from is not None else {}
        return codex.CodexRun(command, instructions=instructions, web=web, images=images, **extra, **options)
    if provider == ollama.PROVIDER:
        if execution is not None:
            # Images are explicit uploads; send their checked bytes to the
            # local inference service, while file tools stay inside the VM.
            options['image_data'] = [execution.call('exec', execution.name, 'python3', '-I', '-c',
                                                   'import base64,sys; print(base64.b64encode(open(sys.argv[1],"rb").read()).decode())', path).strip()
                                     for path in images]
            images = []
        return ollama.OllamaRun(command, instructions=instructions, web=web, images=images, **options)
    module(provider)  # an unknown provider is refused here
    extra = ["--append-system-prompt", instructions] if instructions else []
    paths = PurePosixPath if execution is not None else Path
    for folder in sorted({str(paths(a["path"]).parent) for a in attachments}):
        extra += ["--add-dir", folder]  # outside the working folder: read without a prompt
    return claude_code.ClaudeCodeRun(command, extra_args=extra,
                                     disallowed=() if web else claude_code.WEB_TOOLS, **options)


def lost_session(provider, error):
    """The CLI no longer has the session to resume (deleted, other account, other PC)."""
    if provider == codex.PROVIDER:
        return codex.lost_session(error)
    if provider == ollama.PROVIDER:
        return False  # no session at all
    return "no conversation found" in (error or "").lower()


def stateless(provider):
    """True for a provider that keeps no session: the memory goes with every request."""
    return getattr(module(provider), "STATELESS", False)
