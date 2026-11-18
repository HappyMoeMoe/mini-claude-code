"""Terminal summaries must redact arguments without changing executable calls."""
from copy import deepcopy

import pytest

from mini_claude_code.console import ConsoleBroker
from mini_claude_code.core.messages import ToolCall


@pytest.mark.parametrize('arguments', [
    {'api_key': 'synthetic-private-value'},
    {'options': {'Authorization': 'synthetic-private-value'}},
    {'items': [{'password': 'synthetic-private-value'}]},
    {'access_token': ['synthetic-private-value']},
])
def test_console_redacts_sensitive_fields_recursively(arguments, capsys):
    original = deepcopy(arguments)
    ConsoleBroker().display('call', ToolCall('probe', 'mcp__demo__query', arguments))
    output = capsys.readouterr().out
    assert 'synthetic-private-value' not in output
    assert '<redacted>' in output
    assert arguments == original


def test_team_queue_receives_only_redacted_call(make_runtime):
    from queue import Queue
    from types import SimpleNamespace
    from conftest import ScriptedModelClient
    from mini_claude_code.teams.manager import TeamManager

    sink = SimpleNamespace(console=Queue(), member=lambda agent: {'name': 'worker'},
                           wake_lead=lambda: None)
    runtime = make_runtime(ScriptedModelClient(),
                           display=lambda k, v: TeamManager.display(sink, 'worker', k, v))
    call = ToolCall('team-call', 'bash', {
        'command': 'echo offline-test-key',
        'options': {'credential': 'synthetic-team-private'},
    })
    original = deepcopy(call.arguments)
    runtime.display('call', call)
    output = sink.console.get_nowait()
    assert 'offline-test-key' not in output
    assert 'synthetic-team-private' not in output
    assert '<redacted>' in output and 'echo' in output
    assert call.arguments == original


@pytest.mark.parametrize('field', [
    'passwd', 'PASSWD', 'db_passwd', 'passphrase',
    'private_key', 'privateKey', 'PRIVATE-KEY', 'private.key', 'private key',
    'accessKey', 'signing_key', 'encryption_key', 'api__key',
    '密码', '口令', '私钥', '密钥', '凭证',
])
def test_console_redacts_common_credential_names(field, capsys):
    arguments = {'options': [{'label': 'visible', field: ['synthetic-sensitive-marker']}]}
    original = deepcopy(arguments)
    ConsoleBroker().display('call', ToolCall('probe', 'mcp__demo__login', arguments))
    output = capsys.readouterr().out
    assert 'synthetic-sensitive-marker' not in output
    assert '<redacted>' in output and 'visible' in output
    assert arguments == original


def test_ordinary_keys_and_public_keys_remain_visible(capsys):
    arguments = {'key': 'lookup-id', 'public_key': 'public-material',
                 'keyboard': 'layout', 'monkey': 'animal', 'path': '/tmp/file'}
    ConsoleBroker().display('call', ToolCall('probe', 'query', arguments))
    output = capsys.readouterr().out
    for value in arguments.values():
        assert value in output
    assert '<redacted>' not in output
