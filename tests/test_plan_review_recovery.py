"""Invalid review references are recoverable; damaged authorities remain fatal."""
import json
from pathlib import Path

import pytest

from conftest import ScriptedModelClient, answer
from test_teams_worktrees import call, response, task, spawn, invoke, eventually
from mini_claude_code.core._validation import new_id
from mini_claude_code.core.errors import StorageError
from mini_claude_code.core.loop import TurnOutcome, validate_history
from mini_claude_code.core.messages import ToolResult, ToolStatus


def results(runtime):
    return [b for m in runtime.state.messages for b in m.content if isinstance(b, ToolResult)]


@pytest.mark.parametrize('approve', [True, False])
def test_unknown_plan_returns_error_and_preserves_current_assignment(config, make_runtime, approve):
    client = ScriptedModelClient(response(
        call('review_plan', request_id=new_id('request'), approve=approve),
        call('write_file', path='continued.txt', content='continued')), answer())
    lead = make_runtime(client, teams=True)
    record = task(lead)
    lead.tasks.claim_host(record.task_id)
    before = lead.tasks.store.snapshot()[record.task_id]
    assert lead.run('Review the request and continue') == TurnOutcome.COMPLETED
    assert [r.status for r in results(lead)] == [ToolStatus.ERROR, ToolStatus.SUCCESS]
    assert '当前团队' in results(lead)[0].output
    assert (config.workspace / 'continued.txt').read_text() == 'continued'
    assert lead.tasks.store.snapshot()[record.task_id] == before
    assert len(client.requests) == 2
    validate_history(lead.state.messages)


def test_old_plan_after_resume_is_rejected_without_changing_old_decision(config, make_runtime):
    child = ScriptedModelClient(response(call('submit_plan', plan='Inspect and report.')))
    lead = make_runtime(ScriptedModelClient(), teams=True, child_client_factory=lambda cfg: child)
    worker = spawn(lead, task_id=task(lead).task_id, require_plan=True)
    request_id = eventually(lambda: lead.team.member(worker.agent_id)['latest_plan'])
    session_id, old_store = lead.state.session_id, lead.team.store
    lead.close()
    client = ScriptedModelClient(response(call('review_plan', request_id=request_id, approve=True)),
                                 answer('Request belongs to the previous team.'))
    restored = make_runtime(client, resume=session_id, teams=True)
    old_path = old_store.path('requests/' + request_id + '.json')
    before = old_path.read_bytes()
    assert restored.run('Review the previous plan') == TurnOutcome.COMPLETED
    assert results(restored)[-1].status == ToolStatus.ERROR and len(client.requests) == 2
    assert old_path.read_bytes() == before and not restored.team.workers
    validate_history(restored.state.messages)


def test_foreign_plan_is_recoverable_and_rightful_lead_can_still_approve(make_runtime):
    child = ScriptedModelClient(response(call('submit_plan', plan='Read current task.')), answer())
    owner = make_runtime(ScriptedModelClient(), teams=True, child_client_factory=lambda cfg: child)
    worker = spawn(owner, task_id=task(owner).task_id, require_plan=True)
    request_id = eventually(lambda: owner.team.member(worker.agent_id)['latest_plan'])
    before = owner.team.store.request(request_id)
    client = ScriptedModelClient(response(call('review_plan', request_id=request_id, approve=True)), answer())
    other = make_runtime(client, teams=True)
    assert other.run('Review foreign request') == TurnOutcome.COMPLETED
    assert results(other)[-1].status == ToolStatus.ERROR
    assert owner.team.store.request(request_id) == before
    assert owner.team.member(worker.agent_id)['plan_status'] == 'pending'
    assert invoke(owner, 'review_plan', request_id=request_id, approve=True).status == ToolStatus.SUCCESS
    eventually(lambda: owner.team.member(worker.agent_id)['plan_status'] == 'approved')


@pytest.mark.parametrize('reference', ['latest_plan', 'shutdown_request'])
def test_missing_referenced_request_remains_storage_failure(make_runtime, reference):
    lead = make_runtime(ScriptedModelClient(), teams=True)
    request_id, agent_id = new_id('request'), new_id('agent')
    with lead.team.store.transaction() as data:
        data['members'][agent_id] = {
            'agent_id': agent_id, 'session_id': new_id('session'), 'name': 'stopped',
            'state': 'STOPPED', 'plan_status': 'pending', 'latest_plan': None,
            'shutdown_request': None, reference: request_id}
    with pytest.raises(StorageError):
        invoke(lead, 'review_plan', request_id=request_id, approve=True)


@pytest.mark.parametrize('damage', ['json', 'array', 'sender', 'missing_sender', 'team'])
def test_existing_damaged_request_remains_storage_failure(make_runtime, damage):
    lead = make_runtime(ScriptedModelClient(), teams=True)
    request_id = new_id('request')
    row = {'schema_version': 1, 'request_id': request_id, 'team_id': lead.team_id,
           'type': 'plan', 'status': 'pending', 'sender': new_id('agent'),
           'reviewer': lead.context.agent_id}
    if damage == 'sender': row['sender'] = 'invalid'
    if damage == 'missing_sender': del row['sender']
    if damage == 'team': row['team_id'] = new_id('team')
    path = lead.team.store.path('requests/' + request_id + '.json')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{' if damage == 'json' else json.dumps([] if damage == 'array' else row))
    before = path.read_bytes()
    try:
        # Exercise review itself; boundary discovery must not mask its classification.
        lead.executor.boundary = None
        with pytest.raises(StorageError):
            invoke(lead, 'review_plan', request_id=request_id, approve=True)
        assert path.read_bytes() == before
    finally:
        path.unlink()


def test_unreadable_lookup_is_not_classified_as_missing(make_runtime, monkeypatch):
    lead = make_runtime(ScriptedModelClient(), teams=True)
    request_id = new_id('request')
    path = lead.team.store.path('requests/' + request_id + '.json')
    original = Path.stat
    def inaccessible(self, *args, **kwargs):
        if self == path: raise PermissionError('unreadable authority')
        return original(self, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'stat', inaccessible)
        with pytest.raises(StorageError):
            invoke(lead, 'review_plan', request_id=request_id, approve=True)


def test_request_disappearing_after_lookup_remains_storage_failure(make_runtime, monkeypatch):
    import mini_claude_code.teams.store as module
    lead = make_runtime(ScriptedModelClient(), teams=True)
    request_id = new_id('request')
    path = lead.team.store.path('requests/' + request_id + '.json')
    lead.team.store.save_request({'request_id': request_id})
    original = module.read_json
    def disappeared(workspace, candidate, **kwargs):
        if candidate == path: path.unlink()
        return original(workspace, candidate, **kwargs)
    lead.executor.boundary = None
    with monkeypatch.context() as patch:
        patch.setattr(module, 'read_json', disappeared)
        with pytest.raises(StorageError):
            invoke(lead, 'review_plan', request_id=request_id, approve=True)
