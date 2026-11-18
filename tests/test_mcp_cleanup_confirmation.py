"""Actual MCP processes must stop before cleanup may release task authority."""
import asyncio
from contextlib import contextmanager
import json
from pathlib import Path
import sys

import pytest

from conftest import ScriptedModelClient
from test_teams_worktrees import invoke, task
from mini_claude_code.core.errors import StorageError
from mini_claude_code.core.cancellation import CancellationToken, Cancelled
from mini_claude_code.core.messages import ToolStatus
from mini_claude_code.mcp.client import StdioConnection
from mini_claude_code.mcp import client as transport
from mini_claude_code.mcp.config import MCPError
from mini_claude_code.runtime import Runtime


@contextmanager
def real_runtime(config):
    server = Path(__file__).resolve().parents[1] / 'examples/mcp_server.py'
    path = config.workspace / 'mcp.json'
    path.write_text(json.dumps({'schema_version': 1, 'servers': {'demo': {
        'command': sys.executable, 'args': [str(server)],
        'tools': {'query': {'policy': 'allow', 'effect': 'write'}}}}}))
    runtime = Runtime(config, client=ScriptedModelClient(), stream=False,
                      interactive=False, display=lambda *args: None, mcp_config=str(path))
    try:
        yield runtime
    finally:
        # Failed cleanup fixtures still own real resources. Reap them explicitly
        # without granting the product permission to resume its uncertain task.
        for entry in runtime.mcp.entries.values():
            connection = entry.connection
            runtime.mcp.hub.submit(StdioConnection._reap(connection)).result(timeout=5)
            assert connection.process is None or connection.process.returncode is not None
        try:
            runtime.close()
        except StorageError:
            assert runtime.cleanup_uncertain
        for entry in runtime.mcp.entries.values():
            connection = entry.connection
            assert connection.actor is None or connection.actor.done()
            if connection.slot:
                runtime.mcp.hub.capacity.release()
                connection.slot = False
        runtime.mcp.hub.close()


def test_cancelled_reap_cannot_complete_task_or_release_connection_capacity(config):
    with real_runtime(config) as runtime:
        record = task(runtime)
        runtime.tasks.claim_host(record.task_id)
        before = runtime.tasks.store.snapshot()[record.task_id]
        runtime.mcp.connect('demo')
        connection = runtime.mcp.entries['demo'].connection
        original = connection._reap
        async def interrupted():
            asyncio.current_task().cancel()
            await asyncio.sleep(0)
            return await original()
        connection._reap = interrupted
        try:
            result = invoke(runtime, 'complete_task', task_id=record.task_id)
            assert result.status == ToolStatus.ERROR
            assert runtime.tasks.store.snapshot()[record.task_id] == before
            assert connection.process.returncode is None and not connection.confirmed
            assert runtime.cleanup_uncertain and runtime.mcp.hub.capacity.count == 1
            assert not connection.close()
        finally:
            connection._reap = original


def test_live_connection_unconfirmed_and_normal_close_is_idempotent(config):
    with real_runtime(config) as runtime:
        runtime.mcp.connect('demo')
        connection = runtime.mcp.entries['demo'].connection
        assert not connection.confirmed and connection.process.returncode is None
        runtime.mcp.disconnect('demo')
        assert connection.confirmed and connection.process.returncode is not None
        assert connection.cleanup.done() and runtime.mcp.hub.capacity.count == 0
        assert connection.close() and connection.close()


def test_repeated_owner_cancellation_does_not_cancel_process_cleanup(config):
    with real_runtime(config) as runtime:
        record = task(runtime)
        runtime.tasks.claim_host(record.task_id)
        runtime.mcp.connect('demo')
        connection = runtime.mcp.entries['demo'].connection
        original = connection._reap
        entered, release = asyncio.Event(), asyncio.Event()
        async def blocked():
            entered.set()
            await release.wait()
            return await original()
        async def cancel_owner():
            await entered.wait()
            try:
                for _ in range(3):
                    connection.actor.cancel()
                    await asyncio.sleep(0)
                    assert not connection.confirmed and not connection.cleanup.cancelled()
                    assert runtime.mcp.hub.capacity.count == 1
            finally:
                release.set()
        connection._reap = blocked
        controller = runtime.mcp.hub.submit(cancel_owner())
        try:
            result = invoke(runtime, 'complete_task', task_id=record.task_id)
            controller.result(timeout=3)
            assert result.status == ToolStatus.SUCCESS, result.output
            assert connection.confirmed and connection.process.returncode is not None
            assert runtime.mcp.hub.capacity.count == 0 and not runtime.cleanup_uncertain
            assert runtime.tasks.store.snapshot()[record.task_id].status == 'completed'
        finally:
            runtime.mcp.hub.loop.call_soon_threadsafe(release.set)
            connection._reap = original


def test_close_timeout_preserves_claim_until_cleanup_is_confirmed(config, monkeypatch):
    with real_runtime(config) as runtime:
        record = task(runtime)
        runtime.tasks.claim_host(record.task_id)
        before = runtime.tasks.store.snapshot()[record.task_id]
        runtime.mcp.connect('demo')
        connection = runtime.mcp.entries['demo'].connection
        original = connection._reap
        release = asyncio.Event()
        async def blocked():
            await release.wait()
            return await original()
        async def stopped(): await asyncio.shield(connection.actor)
        connection._reap = blocked
        try:
            with monkeypatch.context() as patch:
                patch.setattr(transport, 'CLOSE_GRACE_SECONDS', .05)
                patch.setattr(transport, 'CLOSE_CANCEL_SECONDS', .05)
                patch.setattr(transport, 'CLOSE_WAIT_SECONDS', 1)
                result = invoke(runtime, 'complete_task', task_id=record.task_id)
            assert result.status == ToolStatus.ERROR
            assert not connection.confirmed and not connection.actor.done()
            assert connection.slot and runtime.mcp.hub.capacity.count == 1
            assert runtime.cleanup_uncertain and not connection.close()
            assert runtime.tasks.store.snapshot()[record.task_id] == before
            with pytest.raises(StorageError):
                invoke(runtime, 'write_file', path='forbidden.txt', content='bad')
            assert not (config.workspace / 'forbidden.txt').exists()
        finally:
            runtime.mcp.hub.loop.call_soon_threadsafe(release.set)
            runtime.mcp.hub.submit(stopped()).result(timeout=5)
            connection._reap = original
        assert connection.confirmed and connection.close() and runtime.mcp.hub.capacity.count == 0
        assert runtime.cleanup_uncertain  # No automatic resumption after uncertain cleanup.
        assert runtime.tasks.store.snapshot()[record.task_id] == before


@pytest.mark.parametrize('failure', ['exception', 'unconfirmed'])
def test_reap_failure_cleans_io_but_preserves_claim_and_capacity(config, failure):
    with real_runtime(config) as runtime:
        record = task(runtime)
        runtime.tasks.claim_host(record.task_id)
        before = runtime.tasks.store.snapshot()[record.task_id]
        runtime.mcp.connect('demo')
        connection = runtime.mcp.entries['demo'].connection
        original = connection._reap
        async def failed():
            if failure == 'exception': raise RuntimeError('reap fixture failed')
            return False
        async def pending_io():
            return [t for t in asyncio.all_tasks() if not t.done() and
                    t.get_coro().__qualname__ in {'StdioConnection._read', 'StdioConnection._write', 'StdioConnection._stderr'}]
        connection._reap = failed
        try:
            assert invoke(runtime, 'complete_task', task_id=record.task_id).status == ToolStatus.ERROR
            assert runtime.tasks.store.snapshot()[record.task_id] == before
            assert not connection.confirmed and connection.slot
            assert runtime.mcp.hub.capacity.count == 1 and runtime.cleanup_uncertain
            assert not runtime.mcp.hub.submit(pending_io()).result(timeout=2)
        finally:
            connection._reap = original


def test_spawn_failure_without_process_confirms_empty_transport(config, monkeypatch):
    with real_runtime(config) as runtime:
        async def failure(*args, **kwargs): raise OSError('spawn fixture failed')
        with monkeypatch.context() as patch:
            patch.setattr(transport.asyncio, 'create_subprocess_exec', failure)
            with pytest.raises(MCPError): runtime.mcp.connect('demo')
        connection = runtime.mcp.entries['demo'].connection
        assert connection.process is None and connection.confirmed
        assert not connection.slot and runtime.mcp.hub.capacity.count == 0
        assert not runtime.cleanup_uncertain


def test_precancelled_open_never_starts_io_or_uses_capacity(config):
    with real_runtime(config) as runtime:
        connection = StdioConnection(runtime.mcp.hub, runtime.mcp.config.servers['demo'], str(config.workspace))
        token = CancellationToken()
        token.cancel()
        with pytest.raises(Cancelled): connection.open(token)
        assert connection.confirmed and connection.process is None
        assert runtime.mcp.hub.thread is None and runtime.mcp.hub.capacity.count == 0


def test_open_queued_after_close_cannot_spawn_a_late_actor(config):
    with real_runtime(config) as runtime:
        connection = StdioConnection(runtime.mcp.hub, runtime.mcp.config.servers['demo'], str(config.workspace))
        assert connection.close()
        with pytest.raises(MCPError): connection.open()
        with pytest.raises(MCPError): runtime.mcp.hub.submit(connection._open()).result(timeout=2)
        assert connection.actor is None and connection.process is None and runtime.mcp.hub.capacity.count == 0
