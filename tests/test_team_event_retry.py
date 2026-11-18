"""Retry must reject an undeliverable source before changing held authority."""
import pytest

from conftest import ScriptedModelClient, answer
from test_teams_worktrees import invoke, spawn, eventually, response, call, task
from test_event_retry_input import cli_fixture
from mini_claude_code import cli
from mini_claude_code.core._validation import new_id
from mini_claude_code.core.events import RuntimeEvent
from mini_claude_code.core.loop import TurnOutcome, validate_history
from mini_claude_code.core.messages import ToolStatus


def old_report(make_runtime):
    child = ScriptedModelClient(response(call('send_message', to='lead', content='Recovered report evidence')), answer())
    lead = make_runtime(ScriptedModelClient(), teams=True, child_client_factory=lambda cfg: child)
    spawn(lead)
    assert invoke(lead, 'send_message', to='alice', content='Please report').status == ToolStatus.SUCCESS
    row = eventually(lambda: next((r for r in lead.events.store.list(lead.state.session_id)
        if r['event']['payload'].get('content') == 'Recovered report evidence'), None))
    session_id, event_id = lead.state.session_id, row['event']['event_id']
    lead.close()
    return session_id, event_id


@pytest.mark.parametrize('teams', [True, False])
def test_old_team_report_retry_rejected_without_changing_held_record(make_runtime, teams):
    session_id, event_id = old_report(make_runtime)
    client = ScriptedModelClient()
    restored = make_runtime(client, resume=session_id, teams=teams)
    path = restored.events.store.path(event_id)
    before = path.read_bytes()
    assert restored.events.store.get(event_id)['status'] == 'held'
    for _ in range(2):
        with pytest.raises(ValueError, match='团队'):
            restored.events.retry(event_id)
        assert path.read_bytes() == before
    assert restored.events.dispatch() is None and not client.requests
    if teams: assert not restored.team.workers


def test_cli_reports_old_source_rejection_and_continues_chat(config, make_runtime, monkeypatch, capsys):
    session_id, event_id = old_report(make_runtime)
    prompts = ['/events retry ' + event_id, 'Continue normal chat', '/exit']
    before = []
    def prepare(runtime): before.append(runtime.events.store.path(event_id).read_bytes())
    client, runtimes, _ = cli_fixture(config, monkeypatch, prompts, prepare)
    assert cli.main(['chat', '--resume', session_id, '--teams', '--memory', 'off', '--no-stream']) == 0
    captured = capsys.readouterr()
    assert '团队' in captured.out and '不能重试' in captured.out
    assert '事件已交回非交互投递' not in captured.out and '无法继续' not in captured.err
    assert not prompts and len(client.requests) == 1
    runtime = runtimes[0]
    assert runtime.events.store.path(event_id).read_bytes() == before[0]
    assert runtime.events.store.get(event_id)['status'] == 'held'
    assert not any(event_id in (m.origin or {}).get('event_ids', []) for m in runtime.state.messages)
    validate_history(runtime.state.messages)


def test_current_team_message_can_still_be_retried_and_delivered(make_runtime):
    client = ScriptedModelClient(answer())
    lead = make_runtime(client, teams=True)
    result = invoke(lead, 'send_message', to='lead', content='Current report')
    assert result.status == ToolStatus.SUCCESS
    row = next(r for r in lead.events.store.list(lead.state.session_id)
               if r['event']['payload']['content'] == 'Current report')
    event_id = row['event']['event_id']
    lead.events.store.hold(event_id)
    lead.events.retry(event_id)
    assert lead.events.store.get(event_id)['status'] == 'pending'
    assert lead.events.dispatch() == TurnOutcome.COMPLETED
    assert lead.events.store.get(event_id)['status'] == 'accepted' and len(client.requests) == 1
    messages = [m for m in lead.state.messages if event_id in (m.origin or {}).get('event_ids', [])]
    assert messages and all(not m.is_human for m in messages)
    with pytest.raises(ValueError): lead.events.retry(event_id)
    assert len(client.requests) == 1
    validate_history(lead.state.messages)


@pytest.mark.parametrize('kind,typ', [('team_message', 'message'), ('team_control', 'shutdown_request')])
def test_current_team_invalid_sender_remains_held(make_runtime, kind, typ):
    client = ScriptedModelClient()
    runtime = make_runtime(client, teams=True)
    sender = new_id('agent')
    event = RuntimeEvent(kind, runtime.state.session_id, {
        'team_id': runtime.team_id, 'sender': sender, 'target': runtime.context.agent_id,
        'type': typ, 'content': 'Untrusted notification',
        'request_id': new_id('request') if kind == 'team_control' else None},
        source_agent_id=sender, source_id=runtime.team_id)
    runtime.events.store.put(event, held=True)
    path = runtime.events.store.path(event.event_id)
    before = path.read_bytes()
    with pytest.raises(ValueError, match='团队'):
        runtime.events.retry(event.event_id)
    assert path.read_bytes() == before and not client.requests


def test_old_plan_notification_cannot_reactivate_previous_protocol(make_runtime):
    child = ScriptedModelClient(response(call('submit_plan', plan='Inspect and report.')))
    lead = make_runtime(ScriptedModelClient(), teams=True, child_client_factory=lambda cfg: child)
    worker = spawn(lead, task_id=task(lead).task_id, require_plan=True)
    request_id = eventually(lambda: lead.team.member(worker.agent_id)['latest_plan'])
    row = eventually(lambda: next((r for r in lead.events.store.list(lead.state.session_id)
        if r['event']['kind'] == 'team_plan' and r['event']['payload']['request_id'] == request_id), None))
    session_id, old_store = lead.state.session_id, lead.team.store
    lead.close()
    client = ScriptedModelClient()
    restored = make_runtime(client, resume=session_id, teams=True)
    old_request = old_store.path('requests/' + request_id + '.json')
    before = old_request.read_bytes()
    event_id = row['event']['event_id']
    with pytest.raises(ValueError, match='团队'):
        restored.events.retry(event_id)
    assert restored.events.store.get(event_id)['status'] == 'held'
    assert old_request.read_bytes() == before and not restored.team.workers and not client.requests
