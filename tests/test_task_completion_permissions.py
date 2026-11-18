"""Completion idempotency must preserve task scope and executor identity."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from conftest import ScriptedModelClient, answer
from mini_claude_code.core._validation import new_id
from mini_claude_code.core.messages import ToolResult, ToolStatus
from mini_claude_code.storage.task_store import TaskStore
from mini_claude_code.tasks.models import Assignment, TaskError
from test_teams_worktrees import call, eventually, invoke, response, source, spawn, task


def completed_record(store, root, *, team_id=None):
    actor = source()
    record = store.create('Completed work', 'FOREIGN_RECORD_MARKER', actor, team_id=team_id)
    claimed = store.claim(record.task_id, actor)
    assignment = Assignment.from_record(claimed, str(root))
    completed, _, _ = store.complete(record.task_id, assignment, 'original summary', actor)
    return completed, assignment, actor


@pytest.mark.parametrize('scope', ['ungrouped', 'other_team', 'same_team'])
def test_teammate_cannot_use_completion_as_a_task_read_endpoint(config, make_runtime, scope):
    child = ScriptedModelClient()
    lead = make_runtime(ScriptedModelClient(), teams=True, child_client_factory=lambda cfg: child)
    team_id = {'ungrouped': None, 'other_team': new_id('team'), 'same_team': lead.team_id}[scope]
    completed, _, _ = completed_record(lead.tasks.store, config.workspace, team_id=team_id)
    own = task(lead, 'Current assignment')
    child.steps = [response(call('get_task', task_id=completed.task_id),
                            call('complete_task', task_id=completed.task_id, summary='must not replace')),
                   answer()]

    worker = spawn(lead, task_id=own.task_id)

    def results():
        return [b for m in worker.runtime.state.messages for b in m.content if isinstance(b, ToolResult)]

    eventually(lambda: len(child.requests) == 2)
    got, completion = results()
    assert got.status == (ToolStatus.SUCCESS if scope == 'same_team' else ToolStatus.ERROR)
    assert completion.status == ToolStatus.ERROR
    assert 'FOREIGN_RECORD_MARKER' not in completion.output
    assert 'original summary' not in completion.output
    assert not completion.metadata.get('already_completed')
    assert worker.runtime.tasks.assignment.task_id == own.task_id
    assert lead.tasks.store.snapshot()[completed.task_id] == completed


@pytest.mark.parametrize('mismatch', ['agent', 'session', 'other_assignment', 'stale_claim'])
def test_completed_store_record_still_checks_executor_identity(config, mismatch):
    store = TaskStore(config.workspace)
    completed, assignment, actor = completed_record(store, config.workspace)
    if mismatch == 'agent':
        actor = {**actor, 'agent_id': new_id('agent')}
    elif mismatch == 'session':
        actor = {**actor, 'session_id': new_id('session')}
    elif mismatch == 'other_assignment':
        another = store.create('Other current work', '', actor)
        assignment = Assignment.from_record(store.claim(another.task_id, actor), str(config.workspace))
    else:
        assignment = replace(assignment, claim_id=new_id('claim'))

    with pytest.raises(TaskError):
        store.complete(completed.task_id, assignment, 'must not replace', actor)

    assert store.snapshot()[completed.task_id] == completed


@pytest.mark.parametrize('keep_assignment', [False, True])
def test_original_executor_can_repeat_completion_without_mutation(config, keep_assignment):
    store = TaskStore(config.workspace)
    completed, assignment, actor = completed_record(store, config.workspace)

    record, ready, changed = store.complete(
        completed.task_id, assignment if keep_assignment else None, 'must not replace', actor)

    assert record == completed
    assert ready == [] and changed is False
    assert store.snapshot()[record.task_id] == completed


@pytest.mark.parametrize('already_completed', [False, True])
def test_store_enforces_team_scope_even_for_the_original_executor(config, already_completed):
    store = TaskStore(config.workspace)
    actor = source()
    record = store.create('Team-scoped work', '', actor, team_id=new_id('team'))
    claimed = store.claim(record.task_id, actor)
    assignment = Assignment.from_record(claimed, str(config.workspace))
    if already_completed:
        original, _, _ = store.complete(record.task_id, assignment, 'original summary', actor)
    else:
        original = claimed

    with pytest.raises(TaskError, match='本团队'):
        store.complete(record.task_id, assignment, 'must not replace', actor, team_id=new_id('team'))

    assert store.snapshot()[record.task_id] == original


def test_unauthorized_completion_does_not_disconnect_current_task_mcp(config, make_runtime, monkeypatch):
    runtime = make_runtime(ScriptedModelClient())
    completed, _, _ = completed_record(runtime.tasks.store, config.workspace)
    own = task(runtime)
    runtime.tasks.claim_host(own.task_id)
    cleanup = []
    mcp = SimpleNamespace(has_work=lambda task_id: cleanup.append('checked') or False,
                          disconnect_all=lambda: cleanup.append('disconnected'))

    with monkeypatch.context() as patch:
        patch.setattr(runtime, 'mcp', mcp)
        result = invoke(runtime, 'complete_task', task_id=completed.task_id)

    assert result.status == ToolStatus.ERROR
    assert cleanup == []
    assert runtime.tasks.assignment.task_id == own.task_id


def test_owner_tool_retry_returns_same_record_without_mcp_cleanup(config, make_runtime, monkeypatch):
    runtime = make_runtime(ScriptedModelClient())
    own = task(runtime)
    runtime.tasks.claim_host(own.task_id)
    first = invoke(runtime, 'complete_task', task_id=own.task_id, summary='original summary')
    completed = runtime.tasks.store.snapshot()[own.task_id]
    cleanup = []
    mcp = SimpleNamespace(has_work=lambda task_id: cleanup.append('checked') or False,
                          disconnect_all=lambda: cleanup.append('disconnected'))

    with monkeypatch.context() as patch:
        patch.setattr(runtime, 'mcp', mcp)
        repeated = invoke(runtime, 'complete_task', task_id=own.task_id, summary='must not replace')

    assert first.status == repeated.status == ToolStatus.SUCCESS
    assert not first.metadata['already_completed'] and repeated.metadata['already_completed']
    assert runtime.tasks.store.snapshot()[own.task_id] == completed
    assert sum(event.kind == 'task_completed' for event in runtime.store.events) == 1
    assert cleanup == []
