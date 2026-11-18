"""Explicit same-lead reassignment may recover safe old-team tasks on resume."""
from dataclasses import replace
from pathlib import Path
import json

import pytest

from conftest import ScriptedModelClient, answer
from test_teams_worktrees import task, spawn, invoke, response, call, eventually, init_git
from mini_claude_code.core.errors import ModelError
from mini_claude_code.core.messages import ToolStatus


@pytest.mark.parametrize('checkout', [False, True])
def test_failed_old_team_task_can_be_explicitly_reassigned(config, make_runtime, checkout):
    if checkout:
        init_git(config.workspace)
    lead = make_runtime(ScriptedModelClient(), teams=True,
                        child_client_factory=lambda cfg: ScriptedModelClient(ModelError('controlled failure')))
    record = task(lead)
    if checkout:
        lead.tasks.worktrees.create('retained', record.task_id, 'HEAD', lead.tasks.source('host'), team_id=lead.team_id)
    old_worker = spawn(lead, task_id=record.task_id)
    assert old_worker.stopped.wait(5)
    old_record = lead.tasks.store.snapshot()[record.task_id]
    assert old_record.status == 'pending' and old_record.auto_claim_blocked
    if checkout:
        (Path(old_record.worktree['path']) / 'unfinished.txt').write_text('retained edit')
    old_team, session_id = lead.team_id, lead.state.session_id
    lead.close()
    child = ScriptedModelClient(answer())
    restored = make_runtime(ScriptedModelClient(), resume=session_id, teams=True,
                            child_client_factory=lambda cfg: child)
    assert restored.team_id != old_team and not child.requests
    result = invoke(restored, 'spawn_teammate', name='retry', task_id=record.task_id, require_plan=True)
    assert result.status == ToolStatus.SUCCESS, result.output
    worker = restored.team.workers[json.loads(result.output)['agent_id']]
    eventually(lambda: len(child.requests) == 1)
    current = restored.tasks.store.snapshot()[record.task_id]
    assert current.status == 'in_progress' and current.team_id == restored.team_id
    assert current.owner['agent_id'] == worker.agent_id
    assert current.revision == old_record.revision + 1
    assert current.created_by == old_record.created_by and current.worktree == old_record.worktree
    assert current.blocked_by == old_record.blocked_by and not current.auto_claim_blocked
    assert current.owner['claim_id'] != old_record.last_release['owner']['claim_id']
    member = restored.team.member(worker.agent_id)
    assert member['plan_status'] == 'required' and member['latest_plan'] is None
    assert worker.runtime.context.cwd == (current.worktree['path'] if checkout else str(config.workspace))
    if checkout:
        assert (Path(current.worktree['path']) / 'unfinished.txt').read_text() == 'retained edit'
    events = [e for e in worker.runtime.store.events if e.kind == 'task_team_adopted']
    assert len(events) == 1 and events[0].payload['previous_team_id'] == old_team
    assert events[0].payload['team_id'] == restored.team_id


@pytest.fixture
def recovery(config, make_runtime):
    lead = make_runtime(ScriptedModelClient(), teams=True,
                        child_client_factory=lambda cfg: ScriptedModelClient(ModelError('controlled failure')))
    record = task(lead)
    worker = spawn(lead, task_id=record.task_id)
    assert worker.stopped.wait(5)
    session_id, old_team = lead.state.session_id, lead.team_id
    lead.close()
    child = ScriptedModelClient(answer())
    restored = make_runtime(ScriptedModelClient(), resume=session_id, teams=True,
                            child_client_factory=lambda cfg: child)
    return lead, worker, restored, record.task_id, old_team, child


@pytest.mark.parametrize('condition', [
    'foreign_team_lead', 'foreign_creator', 'active_member', 'busy_session',
    'missing_session_lock', 'wrong_session_identity', 'orphan_claim',
    'in_progress', 'completed', 'blocked', 'directory_busy',
])
def test_unsafe_old_team_adoption_preserves_task(recovery, condition):
    from mini_claude_code.core._validation import new_id
    from mini_claude_code.storage.locking import SessionLock
    from mini_claude_code.tasks.models import Assignment
    from mini_claude_code.teams.store import TeamStore
    from test_teams_worktrees import source
    old, worker, restored, task_id, old_team, child = recovery
    store = restored.tasks.store
    authority = TeamStore(restored.config.workspace, old_team)
    old_lock = restored.config.data_dir / 'sessions' / worker.runtime.state.session_id / 'session.lock'
    lock = None
    if condition == 'foreign_team_lead':
        with authority.transaction() as data:
            data['lead_session_id'] = new_id('session')
    elif condition == 'foreign_creator':
        with store._locked():
            current = store._get(store._read_all(), task_id)
            store._write(replace(current, created_by=source()))
    elif condition == 'active_member':
        with authority.transaction() as data:
            data['members'][worker.agent_id]['state'] = 'WORKING'
    elif condition == 'busy_session':
        lock = SessionLock(old_lock)
    elif condition == 'missing_session_lock':
        old_lock.unlink()
    elif condition == 'wrong_session_identity':
        path = old_lock.parent / 'session.json'
        metadata = json.loads(path.read_text())
        metadata['identity']['agent_id'] = new_id('agent')
        path.write_text(json.dumps(metadata))
    elif condition in {'orphan_claim', 'in_progress', 'completed'}:
        target = task_id
        if condition == 'orphan_claim':
            target = store.create('Another old task', '', old.tasks.source('host'), team_id=old_team).task_id
        claimed = store.claim(target, worker.runtime.tasks.source('host'))
        if condition == 'completed':
            store.complete(target, Assignment.from_record(claimed, str(restored.config.workspace)),
                           'original', worker.runtime.tasks.source('host'))
    elif condition == 'blocked':
        blocker = store.create('Old dependency', '', old.tasks.source('host'), team_id=old_team)
        store.add_dependencies(task_id, [blocker.task_id], old.tasks.source('host'))
    elif condition == 'directory_busy':
        assert invoke(restored, 'write_file', path='held.txt', content='held').status == ToolStatus.SUCCESS
    before = store.snapshot()
    try:
        result = invoke(restored, 'spawn_teammate', name='denied', task_id=task_id)
        assert result.status == ToolStatus.ERROR, result.output
        assert store.snapshot() == before
        assert not child.requests
        assert all(not w.thread.is_alive() for w in restored.team.workers.values())
    finally:
        if lock is not None:lock.close()


def test_ordinary_teammate_claim_cannot_adopt_old_team_task(recovery):
    old, old_worker, restored, task_id, old_team, child = recovery
    child.steps = [response(call('claim_task', task_id=task_id)), answer()]
    worker = spawn(restored, name='ordinary')
    assert invoke(restored, 'send_message', to='ordinary', content='Inspect the old task ID.').status == ToolStatus.SUCCESS
    from mini_claude_code.core.messages import ToolResult
    def rejected():
        return next((b for m in worker.runtime.state.messages for b in m.content if isinstance(b, ToolResult)), None)
    result = eventually(rejected)
    assert result.status == ToolStatus.ERROR
    current = restored.tasks.store.snapshot()[task_id]
    assert current.team_id == old_team and current.owner is None


def test_old_team_tasks_are_not_automatically_migrated(recovery, monkeypatch):
    from mini_claude_code.storage.task_store import TaskStore
    import threading
    old, old_worker, restored, task_id, old_team, child = recovery
    scanned = threading.Event()
    original = TaskStore.snapshot
    def snapshot(self, *args, **kwargs):
        graph = original(self, *args, **kwargs)
        if threading.current_thread().name == 'teammate-auto':scanned.set()
        return graph
    monkeypatch.setattr(TaskStore, 'snapshot', snapshot)
    worker = spawn(restored, name='auto', auto_claim=True)
    assert scanned.wait(3)
    current = restored.tasks.store.snapshot()[task_id]
    assert current.team_id == old_team and current.owner is None
    assert not child.requests


def test_old_team_change_during_claim_is_rechecked(recovery, monkeypatch):
    from mini_claude_code.storage.task_store import TaskStore
    from mini_claude_code.teams.store import TeamStore
    old, old_worker, restored, task_id, old_team, child = recovery
    authority = TeamStore(restored.config.workspace, old_team)
    original = TaskStore.claim
    def claim(self, *args, **kwargs):
        if kwargs.get('adopt_scope') is not None:
            with authority.transaction() as data:
                data['members'][old_worker.agent_id]['error'] = 'changed after preflight'
        return original(self, *args, **kwargs)
    monkeypatch.setattr(TaskStore, 'claim', claim)
    before = restored.tasks.store.snapshot()[task_id]
    result = invoke(restored, 'spawn_teammate', name='changed', task_id=task_id)
    assert result.status == ToolStatus.ERROR
    assert restored.tasks.store.snapshot()[task_id] == before
    assert not child.requests


def test_pending_task_with_no_old_workers_can_be_reassigned_and_completed(config, make_runtime):
    old = make_runtime(ScriptedModelClient(), teams=True)
    record = task(old)
    session_id = old.state.session_id
    old.close()
    child = ScriptedModelClient(response(call('complete_task', task_id=record.task_id, summary='new completion')))
    restored = make_runtime(ScriptedModelClient(), resume=session_id, teams=True,
                            child_client_factory=lambda cfg: child)
    assert not child.requests
    worker = spawn(restored, name='finish', task_id=record.task_id)
    current = eventually(lambda: (r if (r := restored.tasks.store.snapshot()[record.task_id]).status == 'completed' else None))
    assert current.team_id == restored.team_id and current.created_by == record.created_by
    assert current.completed_by['owner']['agent_id'] == worker.agent_id
    assert current.completion == 'new completion' and current.revision == record.revision + 2


def test_cancel_before_migrated_claim_write_preserves_team_and_releases_locks(recovery, monkeypatch):
    from mini_claude_code.storage.task_store import TaskStore
    from mini_claude_code.storage.locking import SessionLock
    from mini_claude_code.worktrees.service import DirectoryLease
    old, old_worker, restored, task_id, old_team, child = recovery
    before = restored.tasks.store.snapshot()[task_id]
    original = TaskStore._write
    def write(self, record, cancellation=None):
        if record.task_id == task_id and record.team_id == restored.team_id and record.status == 'in_progress':
            assert cancellation is not None
            cancellation.cancel()
        return original(self, record, cancellation)
    monkeypatch.setattr(TaskStore, '_write', write)
    result = invoke(restored, 'spawn_teammate', name='cancelled', task_id=task_id)
    assert result.status == ToolStatus.ERROR
    assert restored.tasks.store.snapshot()[task_id] == before
    assert not child.requests
    path = restored.config.data_dir / 'sessions' / old_worker.runtime.state.session_id / 'session.lock'
    lock = SessionLock(path)
    lock.close()
    lease = DirectoryLease(restored.config.workspace, restored.config.workspace, {'kind': 'check-no-leak'})
    lease.close()
