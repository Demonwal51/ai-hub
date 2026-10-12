"""Docker Sandboxes transport. Only explicitly copied data reaches a microVM.

The engine enforces read-only mounts and network policy. The agent's shell
permissions cannot grant access to the original files or the host accounts.
"""

import hashlib
import json
import os
import re
import shutil
import shlex
import stat
import subprocess
import threading
import uuid
import tempfile
from pathlib import Path

from .providers.claude_code import ProviderError, NO_WINDOW

_LOCKS = {}
_LEASES = {}
_LOGINS = set()
_LOCK = threading.Lock()
MAX_BYTES = 128 * 1024 * 1024
MAX_FILES = 5000
IMAGES = {'claude_code': ('shell', 'docker/sandbox-templates:claude-code'),
          'codex': ('codex', 'docker/sandbox-templates:codex'),
          'ollama': ('shell', 'docker/sandbox-templates:shell-docker')}
ENDPOINTS = {'claude_code': ['api.anthropic.com:443', 'platform.claude.com:443'],
             'codex': ['chatgpt.com:443', 'api.openai.com:443', 'ab.chatgpt.com:443', 'auth.openai.com:443'],
             'ollama': []}
SERVICES = {'claude_code': 'anthropic', 'codex': 'openai'}
# Provider kits also allow registries and developer sites. Deny them at
# creation, before startup hooks run; audit again before starting the AI.
EXTRA_HOSTS = {'downloads.claude.ai:443', 'claude.com:443', 'code.claude.com:443',
               'mcp-proxy.anthropic.com:443', 'bridge.claudeusercontent.com:443',
               'openai.com:443', 'files.openai.com:443', 'registry.npmjs.org:443',
               'releases.openai.com:443', 'api.github.com:443', 'github.com:443',
               'release-assets.githubusercontent.com:443', 'codeload.github.com:443',
               'archive.ubuntu.com:80', 'security.ubuntu.com:80', 'ports.ubuntu.com:80', 'download.docker.com:443'}
FORBIDDEN = {'.git', '.venv', 'node_modules', '.claude', '.codex', '.ssh', '.aws',
             'auth.json', '.credentials.json', 'credentials.json', 'tokens.json', 'secrets.json',
             '.npmrc', '.pypirc', 'id_rsa', 'id_ed25519', 'aihub.db', 'config.json', 'serveur.pid'}


def find_cli(explicit=None):
    found = explicit or shutil.which('sbx')
    if not found and os.name == 'nt':
        candidate = Path(os.environ.get('LOCALAPPDATA', '')) / 'DockerSandboxes/bin/sbx.exe'
        if candidate.is_file():
            found = str(candidate)
    if not found or not Path(found).is_file():
        raise ProviderError('Docker Sandboxes est indisponible : aucun accès au PC ne sera utilisé en remplacement')
    return str(found)


def guest_path(path):
    text = str(Path(path).resolve()).replace('\\', '/')
    if re.match(r'^[A-Za-z]:/', text):
        text = '/' + text[0].lower() + text[2:]
    return text


def _allowed_name(path):
    return not any(p.lower() in FORBIDDEN or p.lower().startswith('.env')
                   or p.lower().endswith(('.key', '.pem', '.p12', '.pfx')) for p in path.parts)


def read_checked(path, root, limit=MAX_BYTES):
    """Check the OPEN handle before reading, closing the check/open symlink race."""
    with Path(path).open('rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ProviderError('fichier non ordinaire ou trop grand')
        if os.name == 'nt':
            import ctypes
            import msvcrt
            from ctypes import wintypes
            function = ctypes.windll.kernel32.GetFinalPathNameByHandleW
            function.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
            function.restype = wintypes.DWORD
            buffer = ctypes.create_unicode_buffer(32768)
            length = function(msvcrt.get_osfhandle(stream.fileno()), buffer, len(buffer), 0)
            if not length or length >= len(buffer):
                raise ProviderError('le chemin réel du fichier ne peut pas être vérifié')
            final = buffer.value
            if final.startswith('\\\\?\\UNC\\'):
                final = '\\\\' + final[8:]
            elif final.startswith('\\\\?\\'):
                final = final[4:]
        else:
            final = os.readlink(f'/proc/self/fd/{stream.fileno()}')
        if not Path(final).resolve().is_relative_to(Path(root).resolve()):
            raise ProviderError('le fichier ouvert dépasse la sélection autorisée')
        data = stream.read(limit + 1)
        if len(data) > limit or len(data) != info.st_size:
            raise ProviderError('le fichier a changé pendant sa lecture')
        return data


def copy_selection(source, destination, selected):
    """Never traverse a symlink/junction; never include credentials or app state."""
    source, destination = Path(source).resolve(), Path(destination)
    found, total, visited = {}, 0, set()
    for selection in selected:
        target = source / selection
        # Check every component BEFORE resolving it or enumerating a directory.
        for item in [target, *target.parents]:
            if item == source:
                break
            if item.is_symlink() or (hasattr(item, 'is_junction') and item.is_junction()):
                raise ProviderError('la sélection contient un lien : choisissez le fichier réel')
        if not target.exists() or not target.resolve().is_relative_to(source):
            raise ProviderError('fichier sélectionné introuvable ou hors du dossier autorisé')
        candidates = [target]
        while candidates:
            item = candidates.pop()
            relative = item.relative_to(source)
            if relative in visited:
                continue
            visited.add(relative)
            if len(visited) > MAX_FILES:
                raise ProviderError('sélection trop grande : 5 000 fichiers et dossiers au maximum')
            if not _allowed_name(relative):
                continue
            if item.is_symlink() or (hasattr(item, 'is_junction') and item.is_junction()):
                raise ProviderError('la sélection contient un lien : copie refusée')
            if item.is_dir():
                (destination / relative).mkdir(parents=True, exist_ok=True)
                for child in item.iterdir():
                    candidates.append(child)
                    if len(candidates) > MAX_FILES:
                        raise ProviderError('sélection trop grande : 5 000 fichiers et dossiers au maximum')
                continue
            if not item.is_file():
                raise ProviderError('seuls les fichiers ordinaires sont copiés')
            if relative.as_posix() in found:
                continue
            size = item.stat().st_size
            total += size
            if total > MAX_BYTES or len(found) >= MAX_FILES:
                raise ProviderError('sélection trop grande : 128 Mo et 5 000 fichiers au maximum')
            # Read a bounded amount, and recheck after reading in case the file changed.
            data = read_checked(item, source)
            if len(data) != size or len(data) > MAX_BYTES:
                raise ProviderError('un fichier a changé pendant la préparation : recommencez')
            if item.is_symlink() or not item.resolve().is_relative_to(source):
                raise ProviderError('un chemin a changé pendant la copie')
            out = destination / relative
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(data)
            found[relative.as_posix()] = hashlib.sha256(data).hexdigest()
    return found


class SandboxExecution:
    def __init__(self, database, conversation, policy, *, account_id=None, read_only=False, settings=None):
        from .config import Settings
        self.settings = settings or Settings()
        self.database = database
        self.conversation = conversation
        self.policy = policy
        self.account_id = account_id
        self.scope_nonce = None
        self.read_only = read_only
        if policy['files'] != 'write':
            self.read_only = False  # already read-only/no files: reuse this person's account
        self.name = None
        self.workdir = '/home/agent/workspace'
        self.cli = None
        self.env = None
        self._leased = False
        self.authentication = False

    def call(self, *args, timeout=90, json_result=False, denied_ok=False):
        try:
            result = subprocess.run([self.cli, *args], env=self.env, stdin=subprocess.DEVNULL,
                                    capture_output=True, text=True, encoding='utf-8', errors='replace',
                                    creationflags=NO_WINDOW, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired):
            raise ProviderError('le moteur isolé ne répond pas ; exécution sur le PC refusée') from None
        if result.returncode and not (denied_ok and result.returncode == 1):
            # Engine errors may contain account diagnostics: keep a short, bounded message.
            raise ProviderError('espace isolé indisponible : ' + (result.stderr or result.stdout)[-500:].strip())
        if json_result:
            try:
                return json.loads(result.stdout)
            except ValueError:
                raise ProviderError('réponse du moteur isolé illisible') from None
        return result.stdout

    def prepare(self, provider, *, create=True):
        if provider not in IMAGES:
            raise ProviderError('ce fournisseur ne prend pas encore en charge les espaces isolés ; aucun repli sur le PC')
        self.cli = find_cli(self.settings.sandbox_cli)
        self.provider = provider
        # No provider account env, API key, SSH agent or credential directory is forwarded.
        self.env = {key: value for key, value in os.environ.items()
                    if not any(word in key.upper() for word in ('TOKEN', 'SECRET', 'API_KEY'))
                    and key not in ('CODEX_HOME', 'CLAUDE_CONFIG_DIR', 'SSH_AUTH_SOCK')}
        if self.settings.sandbox_data:
            self.env['LOCALAPPDATA'] = self.settings.sandbox_data
        self.name, suffix = self.identity(provider)
        with _LOCK:
            lock = _LOCKS.setdefault(self.name, threading.Lock())
        with lock:
            self.call('daemon', 'status')
            # A global credential would share the server owner's account across users.
            global_secrets = self.call('secret', 'ls', '--global', '--json', json_result=True)
            if any(global_secrets.get(key) for key in ('secrets', 'custom_secrets', 'env_only_count')):
                raise ProviderError('le moteur contient des identifiants globaux : utilisez un moteur dédié sans comptes partagés')
            if self.call('mcp', 'ls').strip() != 'No MCP servers registered':
                raise ProviderError('le moteur dédié aux espaces isolés ne doit pas exposer d’outils MCP du PC')
            settings = self.call('settings', 'get', 'ssh.agentForwardingEnabled').strip()
            if settings.lower() not in ('false', 'ssh.agentforwardingenabled: false'):
                raise ProviderError('désactivez la transmission SSH du moteur avant de créer un espace isolé')
            for key, expected in (('mcp.forceLocalGateway', 'true'), ('clipboard.imagePaste', 'false'),
                                  ('claude.remoteControl', 'false')):
                if self.call('settings', 'get', key).strip().lower() != expected:
                    raise ProviderError('le réglage du moteur dépasse les accès choisis : ' + key)
            try:
                info = self.call('inspect', self.name, '--json', json_result=True)
            except ProviderError as exc:
                if 'not found' not in str(exc).lower() and 'does not exist' not in str(exc).lower():
                    raise
                info = None
            root = Path(self.database).resolve().parent / 'espaces-isoles' / suffix
            snapshot, scratch = root / 'selection', root / 'travail'
            access = 'read' if (self.read_only or provider == 'ollama') and self.policy['files'] != 'none' else self.policy['files']
            if info is None:
                if not create:
                    return None
                if root.exists():
                    if root.is_symlink() or (hasattr(root, 'is_junction') and root.is_junction()) or root.resolve().parent != root.parent.resolve():
                        raise ProviderError('le stockage de cet espace contient un lien inattendu')
                    # Preserve interrupted preparation/old output, never erase it.
                    root.rename(root.with_name(root.name + '-avant-' + uuid.uuid4().hex[:8]))
                root.mkdir(parents=True, exist_ok=True, mode=0o700)
                snapshot.mkdir(exist_ok=True, mode=0o700)
                scratch.mkdir(exist_ok=True, mode=0o700)
                manifest = copy_selection(self.conversation['workdir'], snapshot, self.policy['paths'])
                (root / 'manifest.json').write_text(json.dumps(manifest, sort_keys=True), encoding='utf-8')
                agent, image = IMAGES[provider]
                command = ['create', '--name', self.name, '--cpus', '2', '--memory', '2g', '--skills', 'off',
                           '--static-mcp', '', '--pull', 'never', '--template', image]
                for host in (['**'] if self.policy['network'] == 'none' or provider == 'ollama' else sorted(EXTRA_HOSTS)):
                    command.extend(['--deny-network', host])
                command.append(agent)
                if access == 'write':
                    command.append(str(snapshot))
                elif access == 'read':
                    command.extend([str(scratch), str(snapshot) + ':ro'])
                # No implicit download: image installation is a separate administrator action.
                self.call(*command, timeout=180)
                info = self.call('inspect', self.name, '--json', json_result=True)
            expected = str(snapshot) if access == 'write' else str(scratch) if access == 'read' else None
            if info.get('workspace') != expected or info.get('runtime_mounts'):
                raise ProviderError('les montages du moteur ne correspondent pas aux accès choisis')
            if info.get('image') != IMAGES[provider][1] or info.get('agent') != IMAGES[provider][0] or info.get('kits'):
                raise ProviderError('l’image ou le profil de cet espace ne correspond pas au fournisseur choisi')
            extras = info.get('additional_workspaces') or []
            wanted = [{'dir': str(snapshot), 'read_only': True}] if access == 'read' else []
            if extras != wanted:
                raise ProviderError('un montage supplémentaire n’a pas été autorisé')
            scoped = self.call('secret', 'ls', '--sandbox', self.name, '--json', json_result=True)
            own_services = {entry.get('service') or entry.get('name') for entry in scoped.get('secrets', [])}
            service = SERVICES.get(provider)
            permitted = {'mcpgateway'} | ({service} if service and service in own_services else set())
            if any(secret.get('name') not in permitted for secret in info.get('secrets', [])):
                raise ProviderError('cet espace reçoit un compte du serveur : connexion personnelle requise')
            self.workdir = guest_path(snapshot) if access != 'none' else '/home/agent/workspace'
            rules = self.call('policy', 'ls', self.name, '--json', json_result=True).get('rules', [])
            allowed = ENDPOINTS[provider] if self.policy['network'] == 'ai' else []
            active = [r for r in rules if r.get('status') == 'active' and r.get('resource_type') == 'network']
            probe = self.call('policy', 'check', 'network', '--sandbox', self.name, '--json',
                              'policy-probe.aihub.invalid:443', json_result=True, denied_ok=True)
            explicit_closed = any(rule.get('decision') == 'deny' and '**' in rule.get('resources', [])
                                  and rule.get('scope') == f'sandbox:{self.name}' for rule in active)
            if probe.get('allowed') is not False or (probe.get('deny_kind') != 'implicit' and not explicit_closed):
                raise ProviderError('le réseau du moteur doit être fermé par défaut (policy init deny-all)')
            for rule in active:
                if rule.get('decision') != 'allow':
                    continue
                if rule.get('scope') != f'sandbox:{self.name}':
                    raise ProviderError('une règle réseau globale dépasse les accès choisis')
                for host in set(rule.get('resources', [])) - set(allowed):
                    decision = self.call('policy', 'check', 'network', '--sandbox', self.name, '--json', host,
                                         json_result=True, denied_ok=True)
                    if decision.get('allowed') is not False:
                        raise ProviderError('une règle réseau du moteur dépasse les accès choisis')
            existing = {resource for rule in active if rule.get('decision') == 'allow' for resource in rule.get('resources', [])}
            for host in set(allowed) - existing:
                self.call('policy', 'allow', 'network', '--sandbox', self.name, host)
        return self

    def identity(self, provider):
        transport = 'guest-claude-v4' if provider == 'claude_code' else 'proxy-transport-v3'
        value = json.dumps([transport, self.database, self.conversation['id'], self.scope_nonce, self.account_id or 0,
                            str(Path(self.conversation['workdir']).resolve()), provider, self.read_only, self.policy], sort_keys=True)
        suffix = hashlib.sha256(value.encode()).hexdigest()[:16]
        return f'aihub-{self.conversation["id"]}-{suffix}', suffix

    def transport(self, command, *, guest_env=None):
        """An argv-only invocation for the guest (also used by official login flows)."""
        if command and command[0] == 'claude':
            command = ['env', '-u', 'ANTHROPIC_API_KEY', '-u', 'CLAUDE_CODE_OAUTH_TOKEN', *command]
        elif command and command[0] == 'codex':
            command = ['env', '-u', 'OPENAI_API_KEY', *command]
        if guest_env:
            command = ['env', *[f'{key}={value}' for key, value in guest_env.items()], *command]
        if any(arg == '' for arg in command):
            # sbx rejects empty argv elements. POSIX quoting preserves Claude's
            # --tools "" inside the guest; the host never evaluates this shell.
            command = ['sh', '-c', 'exec ' + shlex.join(command)]
        return [self.cli, 'exec', '-i', '--workdir', self.workdir, self.name, *command]

    def spawn(self, command, **kwargs):
        # Every argument is an argv entry; no host shell interprets the guest command.
        kwargs.pop('cwd', None)
        kwargs['env'] = self.env
        with _LOCKS[self.name]:
            if self.name in _LOGINS and not self.authentication:
                raise ProviderError('terminez la connexion de cet espace avant de lancer une IA')
            proc = subprocess.Popen(self.transport(command), **kwargs)
            _LEASES[self.name] = _LEASES.get(self.name, 0) + 1
            self._leased = True
            return proc

    def begin_login(self):
        with _LOCKS[self.name]:
            if _LEASES.get(self.name):
                raise ProviderError('cet espace travaille : attendez avant de connecter le compte')
            _LOGINS.add(self.name)

    def end_login(self):
        with _LOCKS[self.name]:
            _LOGINS.discard(self.name)

    def finish(self):
        """Stop background guest processes when the last run in this space ends."""
        if self.name is None:
            return
        with _LOCKS[self.name]:
            if not self._leased:
                return
            self._leased = False
            _LEASES[self.name] = max(0, _LEASES.get(self.name, 1) - 1)
            if not _LEASES[self.name]:
                self.stop()

    def copy_inputs(self, attachments):
        """Uploaded files are explicit inputs, copied into the guest, never mounted."""
        if not attachments:
            return [], {}
        incoming = '/home/agent/.aihub-incoming/' + uuid.uuid4().hex
        self.call('exec', self.name, 'mkdir', '-p', incoming)
        mapped, replacements, total = [], {}, 0
        for index, attachment in enumerate(attachments):
            source = Path(attachment['path'])
            label = re.sub(r'^\d+-', '', source.name)
            if not _allowed_name(Path(label)) or source.is_symlink():
                raise ProviderError('une pièce jointe contient un nom réservé aux identifiants : copie refusée')
            size = source.stat().st_size
            total += size
            if size > 20 * 1024 * 1024 or total > MAX_BYTES:
                raise ProviderError('pièces jointes trop grandes pour cet espace')
            # Validate the handle before letting the official copy command open it.
            data = read_checked(source, source.parent, limit=20 * 1024 * 1024)
            target = incoming + f'/{index}-' + source.name
            staging = Path(self.database).resolve().parent / 'espaces-isoles' / 'entrees'
            staging.mkdir(parents=True, exist_ok=True, mode=0o700)
            with tempfile.TemporaryDirectory(dir=staging, prefix='copie-') as temporary:
                ready = Path(temporary) / 'piece'
                ready.write_bytes(data)
                self.call('cp', str(ready), f'{self.name}:{target}')
            mapped.append({**attachment, 'path': target})
            replacements[str(source)] = target
        return mapped, replacements

    def stop(self):
        if self.name and self.cli:
            self.call('stop', self.name, timeout=30)

    def reject_host_tools(self, options):
        if (options.get('mcp_config') or {}).get('mcpServers'):
            raise ProviderError('les outils du PC ne sont pas disponibles dans un espace isolé')

    def file_tool(self, name, arguments):
        if name not in ('read_file', 'list_files', 'search_files'):
            return 'refusé : outil non autorisé dans cet espace'
        encoded = json.dumps(arguments if isinstance(arguments, dict) else {}, ensure_ascii=False)
        if len(encoded) > 8000:
            return 'refusé : arguments trop grands'
        code = Path(__file__).with_name('guest_folder.py').read_text(encoding='utf-8')
        return self.call('exec', '--workdir', self.workdir, self.name, 'python3', '-I', '-c', code,
                         self.workdir, name, encoded, timeout=30)[:20000]
