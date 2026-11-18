"""Muse helpers use a subscription and the project's MCP server."""

import json

import pytest

from decompnet.agents import muse


def write_auth(root, *, version=2, **overrides):
    path = root / muse.AUTH_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    account = {'mechanism': 'oauth', 'storage': 'keychain', 'subs_active': True,
               'api_base_url': 'https://api.meta.ai/v1'} | overrides
    path.write_text(json.dumps({'schema_version': version, 'providers': {'meta': account}}))
    return path


def test_muse_uses_oauth_profile_and_only_project_mcp(tmp_path, monkeypatch):
    home = tmp_path / 'profile-home'
    login = tmp_path / 'login-home'
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('USERPROFILE', str(home))
    monkeypatch.setattr(muse, '_login_home', lambda: login)
    monkeypatch.setattr(muse.sys, 'platform', 'darwin')
    source = write_auth(login / '.config', api_key='unrelated-key')
    source.with_name('settings.json').write_text('{"mcp_servers":{"personal":{}}}')

    cmd, extras = muse.MUSE.session_command('muse', tmp_path, 'http://127.0.0.1:1/mcp', 'tool-token',
                                           'muse-wonder-1.0#low')
    assert cmd[cmd.index('--model') + 1] == 'muse-wonder-1.0'
    assert cmd[cmd.index('--reasoning-effort') + 1] == 'low'
    assert 'run.toolset=["mcp__decomp__*"]' in cmd
    assert 'tool-token' not in ' '.join(cmd) and '--disable-shell' in cmd and '--disable-write' in cmd
    prompt = muse.MUSE.prompt_file(tmp_path, 'multiline\nprompt')
    assert prompt.read_text() == 'multiline\nprompt'

    env = muse.MUSE.session_environment({}, extras | {'META_API_KEY': 'key', 'META_MUSE_API_KEY': 'key2',
                                                     'MUSE_CUSTOM_HEADERS': 'headers', 'PATH': 'bin'})
    assert env[muse.MCP_AUTHORIZATION] == 'Bearer tool-token'
    assert not {'META_API_KEY', 'META_MUSE_API_KEY', 'MUSE_CUSTOM_HEADERS'} & env.keys()
    profile = home / muse.PROFILE
    assert env['XDG_CONFIG_HOME'] == str(profile) and env['HOME'] == str(login)
    assert 'unrelated-key' not in (profile / muse.AUTH_FILE).read_text()
    assert not (profile / 'muse/settings.json').exists()


@pytest.mark.parametrize('overrides', [
    {'mechanism': 'api_key'}, {'subs_active': False}, {'api_base_url': 'https://somewhere-else.invalid/v1'},
])
def test_muse_refuses_credentials_outside_subscription(tmp_path, monkeypatch, overrides):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('USERPROFILE', str(tmp_path))
    monkeypatch.setattr(muse, '_login_home', lambda: tmp_path)
    write_auth(tmp_path / muse.PROFILE, **overrides)
    with pytest.raises(ValueError, match='active subscription sign-in'):
        muse.MUSE.session_environment({}, {'META_API_KEY': 'paygo-key'})


def test_muse_stream_counts_project_tool_intents_and_provider_usage():
    def parse(kind, payload):
        return muse.MUSE.parse_line(json.dumps({'payload_type': kind, 'payload': payload}))

    assert parse('task.lifecycle.proposed', {'event': {'task_kind': 'tool.mcp__decomp__guide'}}) == []
    assert parse('task.lifecycle.side_effect_intent', {'event': {'operation': 'tool:mcp__decomp__guide'}}) == [
        ('tool', 'guide()')]
    assert parse('run.terminal.completed', {'text': 'workflow'}) == [('text', 'workflow')]
    [(kind, usage)] = parse('run.usage', {'models': [
        {'input_tokens': 210, 'cached_tokens': 120, 'output_tokens': 45, 'calls': 2}]})
    assert kind == 'usage'
    assert json.loads(usage) == {'input': 90, 'output': 45, 'cache_read': 120, 'turns': 2}
