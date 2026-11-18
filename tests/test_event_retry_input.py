"""Retry input mistakes keep chat usable without weakening event authorities."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from conftest import ScriptedModelClient, answer
from mini_claude_code import cli
from mini_claude_code.console import ConsoleBroker
from mini_claude_code.core._validation import new_id
from mini_claude_code.core.errors import StorageError
from mini_claude_code.core.events import RuntimeEvent
from mini_claude_code.core.loop import validate_history
from mini_claude_code.runtime import Runtime


def due(runtime):
    clock = datetime(2026, 11, 18, tzinfo=timezone.utc)
    runtime.events.cron.clock = lambda: clock
    runtime.events.cron.schedule(dict(cron='* * * * *', prompt='Report once', timezone='UTC',
                                     durable=True, recurring=False), runtime.context)
    runtime.events.cron.tick_once(clock + timedelta(minutes=1))
    return runtime.events.store.list(runtime.state.session_id)[0]


def cli_fixture(config, monkeypatch, prompts, prepare=None):
    client = ScriptedModelClient(answer())
    runtimes, consumed = [], []
    def factory(cfg, **kwargs):
        runtime = Runtime(cfg, **{**kwargs, 'client': client})
        runtimes.append(runtime)
        if prepare is not None: prepare(runtime)
        return runtime
    def poll(self):
        prompt = prompts.pop(0)
        consumed.append(prompt)
        return prompt
    monkeypatch.setattr('mini_claude_code.runtime.Runtime', factory)
    monkeypatch.setattr(cli, 'load_config', lambda **kwargs: config)
    monkeypatch.setattr(ConsoleBroker, 'poll', poll)
    return client, runtimes, consumed


@pytest.mark.parametrize('command', [
    '/events retry ' + new_id('event'), '/events retry invalid',
    '/events retry ../../outside', '/events retry',
    '/events retry ' + new_id('event') + ' extra', '/events unknown',
    '/events   retry',
])
def test_retry_input_error_keeps_cli_usable(config, monkeypatch, capsys, command):
    prompts = [command, 'Continue normal chat', '/exit']
    client, runtimes, consumed = cli_fixture(config, monkeypatch, prompts)
    assert cli.main(['chat', '--memory', 'off', '--no-stream']) == 0
    output = capsys.readouterr()
    assert '无法继续' not in output.err
    assert not prompts and len(consumed) == 3 and len(client.requests) == 1 and client.closed
    assert not runtimes[0].events.store.list()
    validate_history(runtimes[0].state.messages)


@pytest.mark.parametrize('contents', ['{', '[]'])
def test_existing_corrupt_event_still_stops_cli(config, monkeypatch, capsys, contents):
    event_id = new_id('event')
    path = config.data_dir / 'events' / (event_id + '.json')
    def prepare(runtime):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents)
    prompts = ['/events retry ' + event_id, '/exit']
    client, _, _ = cli_fixture(config, monkeypatch, prompts, prepare)
    assert cli.main(['chat', '--memory', 'off', '--no-stream']) == 1
    assert '无法继续' in capsys.readouterr().err
    assert prompts == ['/exit'] and not client.requests and path.read_text() == contents


def test_foreign_event_rejected_before_source_mutation(make_runtime, monkeypatch):
    owner = make_runtime(ScriptedModelClient())
    row = due(owner)
    owner.events.store.hold(row['event']['event_id'])
    other = make_runtime(ScriptedModelClient())
    path = owner.events.store.path(row['event']['event_id'])
    before = path.read_bytes()
    monkeypatch.setattr(other.events, '_source', lambda *args: pytest.fail('foreign source touched'))
    with pytest.raises(ValueError, match='不属于本会话'):
        other.events.retry(row['event']['event_id'])
    assert path.read_bytes() == before


def test_real_held_cron_can_be_retried_and_accepted_via_cli(config, monkeypatch):
    prompts = []
    saved = []
    def prepare(runtime):
        row = due(runtime)
        event = RuntimeEvent.from_dict(row['event'])
        runtime.events.store.hold(event.event_id)
        runtime.events.cron.delivery(event, 'hold')
        saved.append(event.event_id)
        # None makes the CLI dispatch pending events before reading another input.
        prompts.extend(['/events retry ' + event.event_id, None, '/exit'])
    client, runtimes, _ = cli_fixture(config, monkeypatch, prompts, prepare)
    assert cli.main(['chat', '--memory', 'off', '--no-stream']) == 0
    assert not prompts and len(client.requests) == 1
    runtime = runtimes[0]
    assert runtime.events.store.get(saved[0])['status'] == 'accepted'
    assert next(iter(runtime.events.cron.store.snapshot()['jobs'].values()))['status'] == 'completed'
    validate_history(runtime.state.messages)


def test_internal_missing_event_remains_fatal(make_runtime):
    runtime = make_runtime(ScriptedModelClient())
    with pytest.raises(StorageError):
        runtime.events.store.get(new_id('event'))


def test_retry_permission_failure_is_not_missing(make_runtime, monkeypatch):
    runtime = make_runtime(ScriptedModelClient())
    event_id = new_id('event')
    path = runtime.events.store.path(event_id)
    original = Path.stat
    def denied(self, *args, **kwargs):
        if self == path: raise PermissionError('unreadable event')
        return original(self, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'stat', denied)
        with pytest.raises(StorageError):
            runtime.events.retry(event_id)


def test_event_disappearing_after_lookup_remains_fatal(make_runtime, monkeypatch):
    import mini_claude_code.storage.event_store as module
    runtime = make_runtime(ScriptedModelClient())
    row = due(runtime)
    event_id = row['event']['event_id']
    path = runtime.events.store.path(event_id)
    original = module.read_json
    def disappeared(workspace, candidate, **kwargs):
        if candidate == path: path.unlink()
        return original(workspace, candidate, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(module, 'read_json', disappeared)
        with pytest.raises(StorageError):
            runtime.events.retry(event_id)
