"""Cancelling an unconfirmed spawn must stop only its newly created worker."""
import threading

import pytest

from conftest import ScriptedModelClient, answer
from test_teams_worktrees import call, response, task, spawn
from mini_claude_code.core.cancellation import CancellationToken
from mini_claude_code.core.loop import TurnOutcome
from mini_claude_code.tasks.service import TaskService
from mini_claude_code.teams.manager import Worker


@pytest.mark.parametrize('phase', ['factory', 'claim'])
def test_cancel_unconfirmed_spawn_prevents_late_work(config, make_runtime, monkeypatch, phase):
    entered, release, attempted = threading.Event(), threading.Event(), threading.Event()
    token = CancellationToken()
    def work(request):
        attempted.set()
        return response(call('write_file', path='late.txt', content='must not be written'))
    child = ScriptedModelClient(work, answer())
    def factory(cfg):
        if phase == 'factory':
            entered.set()
            assert release.wait(5)
        return child
    original_claim = TaskService.claim_host
    def claim(self, *args, **kwargs):
        record = original_claim(self, *args, **kwargs)
        if self.runtime.teammate_spec is not None and phase == 'claim':
            entered.set()
            assert release.wait(5)
        return record
    monkeypatch.setattr(TaskService, 'claim_host', claim)
    original_stop = Worker.stop
    def stop(self):
        try:
            return original_stop(self)
        finally:
            release.set()
    monkeypatch.setattr(Worker, 'stop', stop)
    lead_client = ScriptedModelClient()
    lead = make_runtime(lead_client, teams=True, child_client_factory=factory)
    record = task(lead)
    lead_client.steps = [response(call('spawn_teammate', name='new_worker', task_id=record.task_id))]
    def cancel():
        assert entered.wait(5)
        token.cancel()
    thread = threading.Thread(target=cancel)
    thread.start()
    try:
        assert lead.run('Start the worker.', cancellation=token) == TurnOutcome.CANCELLED
        # On old code no stop was requested; release exposes the resulting late work.
        release.set()
        worker = next(iter(lead.team.workers.values()))
        if not worker.stop_requested:
            attempted.wait(3)
        assert worker.stop_requested
        assert worker.stopped.wait(3)
        worker.thread.join(3)
        assert not worker.thread.is_alive()
        assert not child.requests and child.closed
        assert not (config.workspace / 'late.txt').exists()
        current = lead.tasks.store.snapshot()[record.task_id]
        assert current.status == 'pending' and current.owner is None
        if phase == 'claim':
            assert current.auto_claim_blocked
        assert not lead.cleanup_uncertain
        lead_client.steps = [answer()]
        assert lead.run('Continue after cancellation.') == TurnOutcome.COMPLETED
    finally:
        release.set()
        thread.join(3)
        lead.close()


@pytest.mark.parametrize('signal', ['token', 'keyboard'])
def test_cancel_at_ready_boundary_preserves_existing_worker(config, make_runtime, monkeypatch, signal):
    token = CancellationToken()
    existing = ScriptedModelClient()
    child = ScriptedModelClient(response(call('write_file', path='late.txt', content='forbidden')), answer())
    clients = iter([existing, child])
    lead_client = ScriptedModelClient()
    lead = make_runtime(lead_client, teams=True, child_client_factory=lambda cfg: next(clients))
    old = spawn(lead, name='existing')
    original_init = Worker.__init__
    def init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        original_wait = self.started.wait
        def wait(timeout=None):
            ready = original_wait(timeout)
            if ready:
                if signal == 'keyboard':
                    raise KeyboardInterrupt()
                token.cancel()
            return ready
        self.started.wait = wait
    monkeypatch.setattr(Worker, '__init__', init)
    record = task(lead)
    lead_client.steps = [response(call('spawn_teammate', name='new_worker', task_id=record.task_id))]
    assert lead.run('Start new worker.', cancellation=token) == TurnOutcome.CANCELLED
    new = next(w for w in lead.team.workers.values() if w is not old)
    assert new.stopped.is_set() and not new.thread.is_alive()
    assert not new.activated.is_set()
    assert not child.requests and child.closed
    assert not (config.workspace / 'late.txt').exists()
    assert old.thread.is_alive() and not old.stop_requested and not existing.closed
    current = lead.tasks.store.snapshot()[record.task_id]
    assert current.status == 'pending' and current.owner is None and current.auto_claim_blocked


def test_startup_timeout_stops_new_worker(make_runtime, monkeypatch):
    import mini_claude_code.teams.manager as manager
    entered, release = threading.Event(), threading.Event()
    child = ScriptedModelClient(answer())
    def factory(cfg):
        entered.set()
        assert release.wait(5)
        return child
    original_stop = Worker.stop
    def stop(self):
        try:return original_stop(self)
        finally:release.set()
    monkeypatch.setattr(Worker, 'stop', stop)
    monkeypatch.setattr(manager, 'STARTUP_TIMEOUT', .03)
    client = ScriptedModelClient(response(call('spawn_teammate', name='slow_worker')), answer())
    lead = make_runtime(client, teams=True, child_client_factory=factory)
    try:
        assert lead.run('Start worker.') == TurnOutcome.COMPLETED
        worker = next(iter(lead.team.workers.values()))
        assert entered.is_set() and worker.stop_requested and worker.stopped.is_set()
        assert not worker.thread.is_alive() and not child.requests and child.closed
        from mini_claude_code.core.messages import ToolResult
        result = next(b for m in lead.state.messages for b in m.content if isinstance(b, ToolResult))
        assert result.status.value == 'error' and '超时' in result.output
        assert not lead.cleanup_uncertain
    finally:release.set()


def test_thread_start_failure_leaves_no_unjoinable_worker(make_runtime, monkeypatch):
    original_start = threading.Thread.start
    def start(thread):
        if thread.name.startswith('teammate-'):
            raise RuntimeError('controlled thread start failure')
        return original_start(thread)
    monkeypatch.setattr(threading.Thread, 'start', start)
    client = ScriptedModelClient(response(call('spawn_teammate', name='failed_worker')), answer())
    lead = make_runtime(client, teams=True)
    assert lead.run('Start worker.') == TurnOutcome.COMPLETED
    worker = next(iter(lead.team.workers.values()))
    assert worker.thread.ident is None and worker.stopped.is_set()
    assert lead.team.member(worker.agent_id)['state'] == 'FAILED'
    lead.close()


def test_unconfirmed_startup_cleanup_blocks_new_work_and_preserves_claim(config, monkeypatch):
    import mini_claude_code.teams.manager as manager
    from mini_claude_code.runtime import Runtime
    from mini_claude_code.core.errors import StorageError
    from mini_claude_code.core.messages import ToolCall
    entered, release = threading.Event(), threading.Event()
    token = CancellationToken()
    original_claim = TaskService.claim_host
    def claim(self, *args, **kwargs):
        record = original_claim(self, *args, **kwargs)
        if self.runtime.teammate_spec is not None:
            entered.set()
            assert release.wait(5)
        return record
    monkeypatch.setattr(TaskService, 'claim_host', claim)
    monkeypatch.setattr(manager, 'STARTUP_STOP_TIMEOUT', .03)
    child = ScriptedModelClient(answer())
    client = ScriptedModelClient()
    lead = Runtime(config, interactive=False, client=client, stream=False,
                   teams=True, child_client_factory=lambda cfg: child)
    record = task(lead)
    client.steps = [response(call('spawn_teammate', name='blocked', task_id=record.task_id))]
    def cancel():
        if entered.wait(5):token.cancel()
    thread = threading.Thread(target=cancel)
    thread.start()
    try:
        with pytest.raises(StorageError, match='收尾未确认'):
            lead.run('Start worker.', cancellation=token)
        worker = next(iter(lead.team.workers.values()))
        assert worker.stop_requested and worker.thread.is_alive()
        assert lead.cleanup_uncertain
        current = lead.tasks.store.snapshot()[record.task_id]
        assert current.status == 'in_progress' and current.owner is not None
        with pytest.raises(StorageError):
            lead.executor.execute(ToolCall('blocked', 'write_file',
                {'path': 'forbidden.txt', 'content': 'bad'}), lead.context)
        assert not (config.workspace / 'forbidden.txt').exists()
    finally:
        release.set()
        thread.join(3)
        worker = next(iter(lead.team.workers.values()))
        worker.thread.join(3)
        assert not worker.thread.is_alive() and not child.requests
        current = lead.tasks.store.snapshot()[record.task_id]
        assert current.status == 'pending' and current.owner is None and current.auto_claim_blocked
        with pytest.raises(StorageError):lead.close()
