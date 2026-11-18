"""Discovery uses one budget through pagination, schema workers and publication."""
import json
import sys
import time
from types import SimpleNamespace

import pytest

from conftest import ScriptedModelClient
from test_mcp import FakeConnection, tool
from mini_claude_code.mcp.config import MCPError
from mini_claude_code.mcp import schema_validation
from mini_claude_code.mcp import adapter, manager


def configured(make_runtime, tmp_path, catalog, budget):
    class Connection(FakeConnection):
        instances = []
    Connection.catalog = catalog
    path = tmp_path / 'mcp.json'
    path.write_text(json.dumps({'schema_version': 1, 'servers': {'demo': {
        'command': sys.executable, 'timeout_seconds': {'list': budget},
        'tools': {t['name']: {'policy': 'allow', 'effect': 'read'} for t in catalog}}}}))
    runtime = make_runtime(ScriptedModelClient(), mcp_config=str(path), mcp_connection_factory=Connection)
    return runtime, Connection


def assert_withdrawn(runtime, connection):
    assert not any(s.name.startswith('mcp__') for s in runtime.registry.specs())
    assert runtime.mcp.entries['demo'].state == 'failed'
    assert connection.closed and not connection.slot


def test_real_schema_discovery_cannot_publish_after_total_timeout(make_runtime, tmp_path):
    runtime, connection = configured(make_runtime, tmp_path, [tool('q' + str(i)) for i in range(8)], .05)
    started = time.monotonic()
    with pytest.raises(MCPError, match='总发现超时'):
        runtime.mcp.connect('demo')
    assert time.monotonic() - started < 1.5
    assert_withdrawn(runtime, connection.instances[0])


@pytest.fixture
def clock(monkeypatch):
    class Clock:
        value = 100.0
        def now(self): return self.value
        def advance(self, seconds): self.value += seconds
    result = Clock()
    for module in (manager, schema_validation):
        monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=result.now))
    return result


def test_input_and_output_definitions_share_one_budget(make_runtime, tmp_path, monkeypatch, clock):
    catalog = [tool('first', outputSchema={'type': 'object'}), tool('second')]
    runtime, connection = configured(make_runtime, tmp_path, catalog, 1)
    deadlines = []
    def consume(schema, *, cancellation=None, deadline=None):
        deadlines.append(deadline)
        clock.advance(.4)
        schema_validation.check_deadline(deadline)
    monkeypatch.setattr(adapter, 'check_schema', consume)
    with pytest.raises(MCPError, match='总发现超时'):
        runtime.mcp.connect('demo')
    assert deadlines == [101.0] * 3
    assert_withdrawn(runtime, connection.instances[0])


def test_last_page_returning_after_deadline_never_starts_schema_work(
        make_runtime, tmp_path, monkeypatch, clock):
    runtime, connection = configured(make_runtime, tmp_path, [tool('first'), tool('second')], 1)
    connection.pages = {
        None: {'tools': [tool('first')], 'nextCursor': 'next'},
        'next': {'tools': [tool('second')]}}
    original = connection.operation
    remaining = []
    def listing(self, kind, cursor, *args, **kwargs):
        remaining.append(kwargs['timeout'])
        clock.advance(.6 if cursor is None else .5)
        return original(self, kind, cursor, *args, **kwargs)
    monkeypatch.setattr(connection, 'operation', listing)
    monkeypatch.setattr(adapter, 'schema_validator', lambda *a, **kw: pytest.fail('schema started after expiry'))
    with pytest.raises(MCPError, match='总发现超时'):
        runtime.mcp.connect('demo')
    assert remaining == pytest.approx([1, .4])
    assert_withdrawn(runtime, connection.instances[0])


@pytest.mark.parametrize('refresh', [False, True])
def test_publication_timeout_removes_partial_and_old_directory(
        make_runtime, tmp_path, monkeypatch, clock, refresh):
    runtime, connection = configured(make_runtime, tmp_path,
                                     [tool('old'), tool('first'), tool('second')], 1)
    monkeypatch.setattr(adapter, 'check_schema', lambda *a, **kw: None)
    if refresh:
        connection.catalog = [tool('old')]
        runtime.mcp.connect('demo')
        old = runtime.registry.get('mcp__demo__old').binding
    connection.catalog = [tool('first'), tool('second')]
    original = runtime.registry.register
    published = []
    def register(spec, *args, **kwargs):
        original(spec, *args, **kwargs)
        published.append(spec.name)
        clock.advance(.6)
    monkeypatch.setattr(runtime.registry, 'register', register)
    with pytest.raises(MCPError, match='总发现超时'):
        if refresh: runtime.mcp.refresh('demo')
        else: runtime.mcp.connect('demo')
    assert published == ['mcp__demo__first', 'mcp__demo__second']
    assert_withdrawn(runtime, connection.instances[0])
    if refresh:
        with pytest.raises(MCPError, match='stale_binding'):
            runtime.mcp.check_binding(old)


def test_worker_success_after_deadline_is_rejected_and_reaped(monkeypatch, clock):
    original = schema_validation.subprocess.Popen
    workers = []
    def spawn(*args, **kwargs):
        process = original(*args, **kwargs)
        workers.append(process)
        communicate = process.communicate
        def late(*args, **kwargs):
            output = communicate(*args, **kwargs)
            clock.advance(2)
            return output
        process.communicate = late
        return process
    monkeypatch.setattr(schema_validation.subprocess, 'Popen', spawn)
    with pytest.raises(schema_validation.DiscoveryTimeout):
        adapter.schema_validator({'type': 'object'}, deadline=clock.now() + 1)
    assert workers and all(p.poll() is not None and p.stdin.closed and p.stdout.closed for p in workers)


def test_expired_discovery_does_not_start_worker(monkeypatch, clock):
    monkeypatch.setattr(schema_validation.subprocess, 'Popen', lambda *a, **kw: pytest.fail('expired worker started'))
    with pytest.raises(schema_validation.DiscoveryTimeout):
        adapter.schema_validator({'type': 'object'}, deadline=clock.now())


def test_per_schema_cap_can_exclude_one_tool_while_discovery_has_time(
        make_runtime, tmp_path, monkeypatch):
    runtime, _ = configured(make_runtime, tmp_path, [tool('slow'), tool('good')], 3)
    original = schema_validation.subprocess.Popen
    workers = []
    def spawn(command, **kwargs):
        if not workers: command = [sys.executable, '-c', 'import time; time.sleep(10)']
        process = original(command, **kwargs)
        workers.append(process)
        return process
    monkeypatch.setattr(schema_validation, 'VALIDATION_TIMEOUT', .4)
    monkeypatch.setattr(schema_validation.subprocess, 'Popen', spawn)
    info = runtime.mcp.connect('demo')
    assert info['tool_count'] == 1
    assert info['excluded'][0]['raw_tool_name'] == 'slow'
    assert runtime.registry.get('mcp__demo__good') is not None
    assert all(p.poll() is not None for p in workers)


def test_discovery_deadline_is_not_kept_for_later_tool_validation(
        make_runtime, tmp_path, clock):
    runtime, _ = configured(make_runtime, tmp_path, [tool()], 1)
    runtime.mcp.connect('demo')
    validator = runtime.registry.get('mcp__demo__query').binding.input_validator
    clock.advance(10)
    assert adapter.validate(validator, {'n': 2}) == {'n': 2}
    with pytest.raises(MCPError):
        adapter.validate(validator, {'n': 1})


def test_cancellation_during_discovery_is_not_swallowed_as_tool_exclusion(
        make_runtime, tmp_path, monkeypatch):
    from mini_claude_code.core.cancellation import CancellationToken, Cancelled
    runtime, connection = configured(make_runtime, tmp_path, [tool()], 3)
    token = runtime.cancellation = CancellationToken()
    original = schema_validation.subprocess.Popen
    workers = []
    def spawn(command, **kwargs):
        process = original([sys.executable, '-c', 'import time; time.sleep(10)'], **kwargs)
        workers.append(process)
        token.cancel()
        return process
    monkeypatch.setattr(schema_validation.subprocess, 'Popen', spawn)
    try:
        with pytest.raises(Cancelled): runtime.mcp.connect('demo')
    finally:
        runtime.cancellation = None
    assert_withdrawn(runtime, connection.instances[0])
    assert workers and all(p.poll() is not None and p.stdin.closed and p.stdout.closed for p in workers)


@pytest.mark.parametrize('phase', ['input', 'output'])
def test_remaining_budget_kills_schema_worker_and_discards_staged_tools(
        make_runtime, tmp_path, monkeypatch, phase):
    catalog = [tool('first'), tool('second')]
    if phase == 'output': catalog[1]['outputSchema'] = {'type': 'object'}
    runtime, connection = configured(make_runtime, tmp_path, catalog, .8)
    original = schema_validation.subprocess.Popen
    workers = []
    target = 2 if phase == 'input' else 3
    def spawn(command, **kwargs):
        if len(workers) + 1 == target:
            command = [sys.executable, '-c', 'import time; time.sleep(10)']
        process = original(command, **kwargs)
        workers.append(process)
        return process
    monkeypatch.setattr(schema_validation.subprocess, 'Popen', spawn)
    started = time.monotonic()
    with pytest.raises(MCPError, match='总发现超时'):
        runtime.mcp.connect('demo')
    assert time.monotonic() - started < 1.8
    assert len(workers) == target
    assert all(p.poll() is not None and p.stdin.closed and p.stdout.closed for p in workers)
    assert_withdrawn(runtime, connection.instances[0])
