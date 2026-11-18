"""Run the official Muse Code CLI using its browser-authenticated subscription."""

from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path

from . import SAID_CHARS, Agent, number, read_json, tool_name, usage_item

PROMPT_FILE = 'prompt.txt'
MCP_AUTHORIZATION = 'DECOMP_MUSE_MCP_AUTHORIZATION'
AUTH_FILE = Path('muse/auth.json')
PROFILE = Path('.config/decompdecomp/muse-code')
AUTH_LOCK = threading.Lock()
META_BASE = 'https://api.meta.ai/v1'


def _login_home() -> Path:
    if sys.platform == 'darwin':
        import pwd

        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    return Path.home()


def _subscription(auth: dict) -> dict | None:
    providers = auth.get('providers')
    meta = providers.get('meta') if isinstance(providers, dict) else None
    if (auth.get('schema_version') in (1, 2) and isinstance(meta, dict) and meta.get('mechanism') == 'oauth'
            and meta.get('subs_active') is True and str(meta.get('api_base_url') or '').rstrip('/') == META_BASE):
        return meta
    return None


def _profile(login_home: Path) -> Path:
    """Only native auth lives here. Muse's personal settings and MCP servers belong to its usual profile."""
    profile = Path.home() / PROFILE
    target = profile / AUTH_FILE
    with AUTH_LOCK:
        if target.exists() or sys.platform != 'darwin':
            return profile
        # A Keychain record holds a reference, so the native CLI can use and refresh the same login.
        # File-backed credentials stay in the profile where the user signed in.
        source = read_json(login_home / '.config' / AUTH_FILE)
        meta = _subscription(source)
        if meta is None or meta.get('storage') != 'keychain':
            return profile
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fields = ('mechanism', 'storage', 'obtained_via', 'api_base_url', 'subs_active', 'user_full_name', 'user_email')
        account = {key: meta[key] for key in fields if key in meta}
        fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as file:
            json.dump({'schema_version': source['schema_version'], 'providers': {'meta': account}}, file)
    return profile


class MuseCode(Agent):
    id = 'muse'
    name = 'Muse Code'
    command = 'muse'
    vendor = 'Meta'
    family = 'muse'
    install = 'curl -fsSL https://dev.meta.ai/install.sh | bash'
    windows_install = "irm 'https://dev.meta.ai/install.ps1' | iex"
    docs = 'https://dev.meta.ai/docs/muse-code'
    takes_effort = True
    efforts = ('none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra')

    def models(self, path: str | None, run: bool = True) -> list[dict]:
        return [{'id': model, 'name': name, 'note': 'Muse Code subscription'} for model, name in (
            ('muse-ember-1.0', 'Muse Ember'), ('muse-wonder-1.0', 'Muse Wonder'),
        )]

    @property
    def signin(self) -> str:
        if sys.platform == 'darwin':
            return 'Run `muse login` and approve the browser sign-in with your subscribed Meta account.'
        if sys.platform == 'win32':
            return "In PowerShell, run `$env:XDG_CONFIG_HOME = \"$HOME/.config/decompdecomp/muse-code\"; muse login`."
        return 'Run `XDG_CONFIG_HOME="$HOME/.config/decompdecomp/muse-code" muse login` with your subscribed Meta account.'

    def session_command(self, exe, workdir, mcp_url, token, model):
        selected, _, effort = (model or '').partition('#')
        mcp = {'transport': 'streamable_http', 'url': mcp_url, 'mode': 'required',
               'headers': {'Authorization': '${' + MCP_AUTHORIZATION + '}'}}
        # Muse only starts MCP servers for a trusted workspace. This is the helper's private app folder.
        # The empty preset and explicit toolset leave tool calls and permissions with decompdecomp.
        cmd = [exe, 'exec', '--json', '--provider', 'meta', '--preset', 'empty', '--no-session-log',
               '--trust-workspace', '--no-foreign-personal-context', '--disable-shell', '--disable-write', '--disable-web-tools',
               '--disable-reminders', '--disable-request-user-input', '--disable-approval', '--approval-judge', 'off',
               '--contributor-mode', 'off', '--max-model-steps', '100', '--prompt-file', str(workdir / PROMPT_FILE),
               '-c', 'run.toolset=["mcp__decomp__*"]', '-c', 'mcp_servers.decomp=' + json.dumps(mcp)]
        if selected:
            cmd.extend(('--model', selected))
        if effort:
            cmd.extend(('--reasoning-effort', effort))
        return cmd, {MCP_AUTHORIZATION: 'Bearer ' + token}

    def prompt_file(self, workdir: Path, prompt: str) -> Path:
        path = workdir / PROMPT_FILE
        path.write_text(prompt, encoding='utf-8')
        path.chmod(0o600)
        return path

    def session_environment(self, options: dict, env: dict) -> dict:
        login_home = _login_home()
        profile = _profile(login_home)
        if not _subscription(read_json(profile / AUTH_FILE)):
            raise ValueError('Muse Code needs an active subscription sign-in. ' + self.signin)
        if (profile / 'muse/settings.json').exists():
            raise ValueError('Move settings.json out of the dedicated decompdecomp Muse profile. It is used for sign-in only.')
        # CLI API overrides outrank its browser session. Give it only the native login and the app's project token.
        clean = {key: value for key, value in env.items()
                 if not key.upper().startswith(('META_', 'MUSE_', 'TBH_', 'MODEL_API_'))}
        return clean | {'HOME': str(login_home), 'XDG_CONFIG_HOME': str(profile), 'MUSE_NO_AUTO_UPDATE': '1'}

    def parse(self, event, state):
        payload = event.get('payload')
        if not isinstance(payload, dict):
            return []
        kind = event.get('payload_type', '')
        if kind == 'run.terminal.completed':
            return [('text', str(payload.get('text') or '').strip())]
        if kind in ('run.terminal.failed', 'run.terminal.cancelled'):
            return [('error', str(payload.get('reason') or payload.get('text') or kind)[:SAID_CHARS])]
        if kind == 'run.usage':
            return [usage_item(input=max(0, number(row, 'input_tokens') - number(row, 'cached_tokens')),
                               cache_read=number(row, 'cached_tokens'), output=number(row, 'output_tokens'),
                               turns=number(row, 'calls')) for row in payload.get('models', []) if isinstance(row, dict)]
        if kind == 'task.lifecycle.side_effect_intent':
            intent = payload.get('event')
            operation = str(intent.get('operation') or '') if isinstance(intent, dict) else ''
            if operation.startswith('tool:mcp__decomp__'):
                return [('tool', f"{tool_name(operation.removeprefix('tool:'))}()")]
        return []

    def fatal(self, error: str) -> bool:
        return any(term in error.lower() for term in ('muse login', 'invalid model', 'unknown model',
                                                       'refusing run', 'credentials not found'))


MUSE = MuseCode()
