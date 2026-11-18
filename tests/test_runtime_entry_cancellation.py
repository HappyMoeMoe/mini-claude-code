"""Entry cancellation follows the same owned-resource cleanup as loop cancellation."""
import threading

import pytest

from conftest import ScriptedModelClient, answer
from test_teams_worktrees import task, call, response, spawn
from test_shell_cleanup_failure import shell_runtime
from mini_claude_code.agents.factory import AgentSessionFactory
from mini_claude_code.core.cancellation import CancellationToken
from mini_claude_code.core.loop import TurnOutcome, validate_history
from mini_claude_code.core.errors import StorageError
from mini_claude_code.tasks.models import TaskError


@pytest.mark.parametrize('assigned', [False, True])
def test_precancelled_entry_stops_background_and_resets_state(make_runtime, assigned):
    client = ScriptedModelClient(response(call('bash', command='sleep 30', run_in_background=True)), answer())
    runtime = make_runtime(client)
    if assigned:
        record = task(runtime)
        runtime.tasks.claim_host(record.task_id)
    assert runtime.run('Launch one controlled background command') == TurnOutcome.COMPLETED
    entries = list(runtime.events.background.work.values())
    assert entries and any(e.thread.is_alive() for e in entries)
    token = CancellationToken()
    token.cancel()
    before_requests, before_messages = len(client.requests), len(runtime.state.messages)
    assert runtime.run('Never accepted cancelled input', cancellation=token) == TurnOutcome.CANCELLED
    assert all(not e.thread.is_alive() for e in entries)
    assert not runtime.events.background.work
    assert runtime.cancellation is None and runtime.shell.cancellation is None
    assert runtime.tasks.assignment is None and runtime.tasks.lease is None
    assert not runtime.cleanup_uncertain
    assert len(client.requests) == before_requests and len(runtime.state.messages) == before_messages
    if assigned:
        current = runtime.tasks.store.snapshot()[record.task_id]
        assert current.status == 'pending' and current.owner is None
    validate_history(runtime.state.messages)
    client.steps = [answer()]
    assert runtime.run('Continue normally') == TurnOutcome.COMPLETED


def claimed_background(make_runtime, **kwargs):
    client = ScriptedModelClient(response(call('bash', command='sleep 30', run_in_background=True)), answer())
    runtime = make_runtime(client, **kwargs)
    record = task(runtime)
    runtime.tasks.claim_host(record.task_id)
    runtime.run('Launch owned background fixture')
    return runtime, client, record, list(runtime.events.background.work.values())


@pytest.mark.parametrize('stage', ['context', 'skills'])
def test_entry_keyboard_interrupt_uses_normal_cancellation_cleanup(make_runtime, monkeypatch, stage):
    runtime, client, record, entries = claimed_background(make_runtime)
    token = CancellationToken()
    before = len(runtime.state.messages), len(client.requests)
    def interrupt(*args, **kwargs): raise KeyboardInterrupt()
    with monkeypatch.context() as patch:
        patch.setattr(runtime.tasks if stage == 'context' else runtime.skills,
                      'context' if stage == 'context' else 'refresh', interrupt)
        assert runtime.run('Interrupted before loop', cancellation=token) == TurnOutcome.CANCELLED
    assert token.cancelled and runtime.cancellation is None and runtime.shell.cancellation is None
    assert all(not e.thread.is_alive() for e in entries)
    assert runtime.tasks.store.snapshot()[record.task_id].owner is None
    assert (len(runtime.state.messages), len(client.requests)) == before


@pytest.mark.parametrize('stage', ['events', 'context', 'skills', 'subagents'])
def test_entry_error_cleans_resources_and_propagates_original_error(make_runtime, monkeypatch, stage):
    runtime, client, record, entries = claimed_background(make_runtime)
    targets = {'events': (runtime.events, 'start'), 'context': (runtime.tasks, 'context'),
               'skills': (runtime.skills, 'refresh'), 'subagents': (runtime.task_runner, 'begin_turn')}
    target, method = targets[stage]
    before = len(client.requests)
    def failure(*args, **kwargs): raise TaskError('entry preparation fixture failed')
    with monkeypatch.context() as patch:
        patch.setattr(target, method, failure)
        with pytest.raises(TaskError, match='entry preparation fixture failed'):
            runtime.run('Failed before loop')
    assert runtime.cancellation is None and runtime.shell.cancellation is None
    assert all(not e.thread.is_alive() for e in entries) and len(client.requests) == before
    assert runtime.tasks.store.snapshot()[record.task_id].owner is None
    assert runtime.tasks.assignment is None and runtime.tasks.lease is None


def test_cooperative_cancel_during_skill_refresh_is_checked_before_loop(make_runtime, monkeypatch):
    runtime, client, record, entries = claimed_background(make_runtime)
    before = len(client.requests)
    original = runtime.skills.refresh
    def cancelled_refresh():
        runtime.cancellation.cancel()
        return original()
    with monkeypatch.context() as patch:
        patch.setattr(runtime.skills, 'refresh', cancelled_refresh)
        assert runtime.run('Cancelled during refresh') == TurnOutcome.CANCELLED
    assert len(client.requests) == before and all(not e.thread.is_alive() for e in entries)
    assert runtime.tasks.store.snapshot()[record.task_id].owner is None
    assert runtime.cancellation is None and runtime.shell.cancellation is None


def test_idle_boundary_failure_cannot_leave_cancelled_tokens_installed(make_runtime, monkeypatch):
    runtime = make_runtime(ScriptedModelClient())
    token = CancellationToken()
    token.cancel()
    def failure(): raise StorageError('idle boundary fixture failed')
    with monkeypatch.context() as patch:
        patch.setattr(runtime.tasks, 'idle_boundary', failure)
        with pytest.raises(StorageError, match='idle boundary fixture failed'):
            runtime.run('Already cancelled', cancellation=token)
    assert runtime.cancellation is None and runtime.shell.cancellation is None
    assert not runtime.state.messages


def test_cancellation_while_waiting_for_task_graph_lock_still_releases_owned_work(make_runtime):
    runtime, client, record, entries = claimed_background(make_runtime)
    token = CancellationToken()
    held, release = threading.Event(), threading.Event()
    def hold():
        with runtime.tasks.store._locked():
            held.set()
            assert release.wait(3)
    holder = threading.Thread(target=hold)
    holder.start()
    assert held.wait(3)
    def cancel():
        token.cancel()
        release.set()
    timer = threading.Timer(.05, cancel)
    timer.start()
    before = len(client.requests)
    try:
        assert runtime.run('Cancelled while verifying assignment', cancellation=token) == TurnOutcome.CANCELLED
    finally:
        release.set()
        timer.join(3)
        holder.join(3)
    assert not holder.is_alive() and len(client.requests) == before
    assert all(not e.thread.is_alive() for e in entries)
    assert runtime.tasks.store.snapshot()[record.task_id].owner is None
    assert runtime.cancellation is None and runtime.shell.cancellation is None


def test_unconfirmed_entry_cleanup_retains_claim_and_blocks_new_work(config, monkeypatch):
    client = ScriptedModelClient(response(call('bash', command='sleep 30', run_in_background=True)), answer())
    with shell_runtime(config, client) as runtime:
        record = task(runtime)
        runtime.tasks.claim_host(record.task_id)
        runtime.run('Launch background fixture')
        before = runtime.tasks.store.snapshot()[record.task_id]
        entries = list(runtime.events.background.work.values())
        token = CancellationToken()
        token.cancel()
        def unconfirmed(*args, **kwargs):
            runtime.cleanup_uncertain = True
            raise StorageError('background cleanup not confirmed')
        try:
            with monkeypatch.context() as patch:
                patch.setattr(runtime.events, 'close_background', unconfirmed)
                assert runtime.run('Cancelled entry', cancellation=token) == TurnOutcome.CANCELLED
            assert runtime.tasks.store.snapshot()[record.task_id] == before
            assert runtime.tasks.assignment is not None and runtime.tasks.lease is not None
            assert runtime.cancellation is None and runtime.shell.cancellation is None
            assert any(e.thread.is_alive() for e in entries)
            with pytest.raises(StorageError, match='上次执行链未确认停止'):
                runtime.run('Must not continue')
        finally:
            runtime.events.close_background()
            assert all(not e.thread.is_alive() for e in entries)


def test_entry_cancel_keeps_existing_persistent_teammate_running(make_runtime):
    child = ScriptedModelClient()
    client = ScriptedModelClient()
    runtime = make_runtime(client, teams=True, child_client_factory=lambda cfg: child)
    worker = spawn(runtime)
    record = task(runtime)
    runtime.tasks.claim_host(record.task_id)
    token = CancellationToken()
    token.cancel()
    assert runtime.run('Cancel only this lead entry', cancellation=token) == TurnOutcome.CANCELLED
    assert worker.thread.is_alive() and not worker.stop_requested and not child.requests
    assert runtime.tasks.store.snapshot()[record.task_id].owner is None
    assert runtime.cancellation is None and runtime.shell.cancellation is None


def test_child_entry_cancel_never_releases_borrowed_parent_claim(make_runtime):
    parent = make_runtime(ScriptedModelClient())
    record = task(parent)
    parent.tasks.claim_host(record.task_id)
    before = parent.tasks.store.snapshot()[record.task_id]
    client = ScriptedModelClient()
    child = AgentSessionFactory(parent, lambda cfg: client).create('entry-cancel-child', 'analyze')
    token = CancellationToken()
    token.cancel()
    try:
        assert child.run('Never accepted child input', cancellation=token) == TurnOutcome.CANCELLED
        assert not client.requests and not child.state.messages
        assert child.cancellation is None and child.shell.cancellation is None
        assert parent.tasks.store.snapshot()[record.task_id] == before
        assert parent.tasks.assignment is not None and parent.tasks.lease is not None
        parent.tasks.lease.verify()
    finally:
        child.close()


def test_precancelled_empty_entry_does_not_start_services_or_accept_input(make_runtime):
    client = ScriptedModelClient()
    runtime = make_runtime(client)
    token = CancellationToken()
    token.cancel()
    assert runtime.run('Never accepted', cancellation=token) == TurnOutcome.CANCELLED
    assert not runtime.events.started and runtime.events.cron.thread is None
    assert not runtime.state.messages and not client.requests
    assert runtime.cancellation is None and runtime.shell.cancellation is None
