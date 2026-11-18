"""Unconfirmed foreground Shell cleanup must stop the accepted tool batch."""
from contextlib import contextmanager
import selectors
import subprocess
import sys

import pytest

from conftest import ScriptedModelClient, answer
from test_teams_worktrees import task, call, response, invoke
from mini_claude_code.core.errors import StorageError
from mini_claude_code.core.cancellation import Cancelled
from mini_claude_code.core.loop import TurnOutcome, validate_history
from mini_claude_code.core.messages import ToolResult
from mini_claude_code.storage.session_store import SessionStore
from mini_claude_code.storage.task_store import TaskStore
from mini_claude_code.runtime import Runtime
from mini_claude_code.tools.shell import ShellRunner


@contextmanager
def shell_runtime(config, client):
    runtime = Runtime(config, client=client, stream=False, interactive=True,
                      approve=lambda *args: True, display=lambda *args: None)
    try:
        yield runtime
    finally:
        for process in list(runtime.shell._active):
            ShellRunner._stop(process)
            for pipe in (process.stdout, process.stderr):
                if pipe is not None: pipe.close()
            runtime.shell._active.discard(process)
        try:
            try: runtime.close()
            except StorageError: assert runtime.cleanup_uncertain
        finally:
            runtime.tasks.release_lease()  # Fixture processes were explicitly reaped above.


@pytest.mark.parametrize('error', [PermissionError, OSError, KeyboardInterrupt, Cancelled])
def test_stop_failure_blocks_same_batch_completion_and_writes(config, monkeypatch, error):
    client = ScriptedModelClient()
    with shell_runtime(config, client) as runtime:
        record = task(runtime)
        runtime.tasks.claim_host(record.task_id)
        before = runtime.tasks.store.snapshot()[record.task_id]
        client.steps = [response(call('bash', command='sleep 30', timeout_seconds=1),
                                 call('complete_task', task_id=record.task_id),
                                 call('write_file', path='forbidden.txt', content='bad')), answer()]
        def fail(process): raise error('injected process-group cleanup failure')
        original_selector = selectors.DefaultSelector
        created = []
        def selector():
            value = original_selector()
            created.append(value)
            return value
        with monkeypatch.context() as patch:
            patch.setattr(runtime.shell, '_stop', fail)
            patch.setattr(selectors, 'DefaultSelector', selector)
            with pytest.raises(StorageError): runtime.run('Controlled shell cleanup failure')
        assert runtime.tasks.store.snapshot()[record.task_id] == before
        assert not (config.workspace / 'forbidden.txt').exists() and len(client.requests) == 1
        assert runtime.cleanup_uncertain and runtime.shell.unconfirmed_work
        assert runtime.shell._active and any(p.poll() is None for p in runtime.shell._active)
        assert all(p.stdout.closed and p.stderr.closed for p in runtime.shell._active)
        assert created and all(s.get_map() is None for s in created)
        assert runtime.artifacts.foreground_capture == 0
        assert not any(isinstance(b, ToolResult) and b.metadata.get('tool_name') == 'complete_task'
                       for m in runtime.state.messages for b in m.content)


def test_one_time_failure_aborts_batch_and_recovers_history_without_replay(config, monkeypatch):
    client = ScriptedModelClient()
    with shell_runtime(config, client) as runtime:
        record = task(runtime)
        runtime.tasks.claim_host(record.task_id)
        before = runtime.tasks.store.snapshot()[record.task_id]
        client.steps = [response(call('bash', command='sleep 30', timeout_seconds=1),
                                 call('complete_task', task_id=record.task_id),
                                 call('write_file', path='forbidden.txt', content='bad'))]
        original = runtime.shell._stop
        calls = 0
        def fail_once(process):
            nonlocal calls
            calls += 1
            if calls == 1: raise OSError('one-time cleanup failure')
            original(process)
        with monkeypatch.context() as patch:
            patch.setattr(runtime.shell, '_stop', fail_once)
            with pytest.raises(StorageError): runtime.run('Cleanup failed once')
        assert calls >= 2 and not runtime.shell._active
        assert runtime.shell.unconfirmed_work and runtime.cleanup_uncertain
        assert runtime.tasks.store.snapshot()[record.task_id] == before
        assert not (config.workspace / 'forbidden.txt').exists()
        session_id = runtime.state.session_id
    store = SessionStore.resume(config.workspace, session_id)
    try:
        validate_history(store.state.messages)
        results = [b for m in store.state.messages for b in m.content if isinstance(b, ToolResult)]
        assert len(results) == 3
        assert results[0].metadata['execution_unknown']
        assert all(r.status.value == 'cancelled' and not r.metadata['execution_unknown'] for r in results[1:])
        assert TaskStore(config.workspace).snapshot()[record.task_id] == before
        assert not (config.workspace / 'forbidden.txt').exists() and len(client.requests) == 1
    finally:
        store.close()


def test_stop_returning_without_termination_is_not_confirmation(config, monkeypatch):
    client = ScriptedModelClient()
    with shell_runtime(config, client) as runtime:
        record = task(runtime)
        runtime.tasks.claim_host(record.task_id)
        before = runtime.tasks.store.snapshot()[record.task_id]
        client.steps = [response(call('bash', command='sleep 30', timeout_seconds=1),
                                 call('complete_task', task_id=record.task_id))]
        with monkeypatch.context() as patch:
            patch.setattr(runtime.shell, '_stop', lambda process: None)
            with pytest.raises(StorageError): runtime.run('Unconfirmed stop return')
        assert runtime.shell._active and runtime.shell.unconfirmed_work and runtime.cleanup_uncertain
        assert runtime.tasks.store.snapshot()[record.task_id] == before


def test_close_attempts_all_processes_and_keeps_only_unconfirmed_tracking(config, monkeypatch):
    runner = ShellRunner()
    processes = [subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],
                                  cwd=config.workspace, stdin=subprocess.DEVNULL,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
                 for _ in range(2)]
    runner._active.update(processes)
    original = runner._stop
    attempted = []
    def fail_one(process):
        attempted.append(process)
        if process is processes[0]: raise PermissionError('cleanup failure fixture')
        original(process)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(runner, '_stop', fail_one)
            with pytest.raises(StorageError): runner.close()
            assert set(attempted) == set(processes)
            assert runner._active == {processes[0]} and runner.unconfirmed_work
            assert processes[0].poll() is None and processes[1].poll() is not None
            assert all(p.stdout.closed and p.stderr.closed for p in processes)
            with pytest.raises(StorageError): runner.close()
    finally:
        for process in processes:
            original(process)
            process.stdout.close(); process.stderr.close()
            runner._active.discard(process)


def test_activity_guard_blocks_completion_even_without_uncertainty_flag(config):
    with shell_runtime(config, ScriptedModelClient()) as runtime:
        record = task(runtime)
        runtime.tasks.claim_host(record.task_id)
        before = runtime.tasks.store.snapshot()[record.task_id]
        process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],
                                   cwd=config.workspace, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        runtime.shell._active.add(process)
        assert not runtime.shell.unconfirmed_work
        with pytest.raises(StorageError): invoke(runtime, 'complete_task', task_id=record.task_id)
        assert runtime.cleanup_uncertain and runtime.tasks.store.snapshot()[record.task_id] == before
        with pytest.raises(StorageError): invoke(runtime, 'write_file', path='forbidden.txt', content='bad')
        assert not (config.workspace / 'forbidden.txt').exists()


def test_background_shell_cleanup_failure_keeps_task_and_stops_new_work(config, monkeypatch):
    client = ScriptedModelClient(response(call('bash', command='sleep 30', timeout_seconds=1,
                                               run_in_background=True)), answer())
    with shell_runtime(config, client) as runtime:
        record = task(runtime)
        runtime.tasks.claim_host(record.task_id)
        before = runtime.tasks.store.snapshot()[record.task_id]
        original = ShellRunner._stop
        processes = set()
        def failed(process):
            processes.add(process)
            raise PermissionError('background cleanup failure fixture')
        try:
            with monkeypatch.context() as patch:
                patch.setattr(ShellRunner, '_stop', staticmethod(failed))
                assert runtime.run('Launch background fixture') == TurnOutcome.COMPLETED
                for entry in runtime.events.background.work.values(): entry.thread.join(timeout=3)
                with pytest.raises(StorageError): runtime.events.boundary()
            assert runtime.cleanup_uncertain and runtime.events.background.failed
            assert runtime.tasks.store.snapshot()[record.task_id] == before
            with pytest.raises(StorageError): invoke(runtime, 'complete_task', task_id=record.task_id)
            assert processes and all(p.stdout.closed and p.stderr.closed for p in processes)
        finally:
            for process in processes:
                original(process)
                process.stdout.close(); process.stderr.close()
