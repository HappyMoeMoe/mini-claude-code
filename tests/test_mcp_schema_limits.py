"""Schema work is bounded separately from the remote MCP request deadline."""
import subprocess
import sys
from pathlib import Path
import textwrap

import pytest


@pytest.mark.parametrize('phase', ['input', 'output'])
def test_expensive_pattern_does_not_block_runtime(tmp_path, phase):
    script = textwrap.dedent('''
        import json,sys
        from dataclasses import replace
        from pathlib import Path
        sys.path.insert(0,sys.argv[3])
        from conftest import ScriptedModelClient
        from test_mcp import FakeConnection
        from mini_claude_code.config import load_config
        from mini_claude_code.runtime import Runtime
        from mini_claude_code.core.messages import ToolCall,ToolStatus
        root,phase=Path(sys.argv[1]),sys.argv[2]
        pattern={'type':'object','properties':{'value':{'type':'string','pattern':'^(a+)+$'}}}
        definition={'name':'query','inputSchema':pattern if phase=='input' else {'type':'object'}}
        if phase=='output':definition['outputSchema']=pattern
        class Connection(FakeConnection):
            instances=[]
            catalog=[definition]
            response={'content':[],'structuredContent':{'value':'a'*28+'!'}}
        path=root/'mcp.json'
        path.write_text(json.dumps({'schema_version':1,'servers':{'demo':{'command':sys.executable,
            'timeout_seconds':{'call':.1},'tools':{'query':{'policy':'allow','effect':'read'}}}}}))
        cfg=replace(load_config(workspace=root,environ={'DEEPSEEK_API_KEY':'fake-key'},env_file=root/'absent.env'),memory_enabled=False)
        runtime=Runtime(cfg,interactive=False,client=ScriptedModelClient(),mcp_config=str(path),mcp_connection_factory=Connection)
        try:
            runtime.mcp.connect('demo')
            args={'value':'a'*28+'!'} if phase=='input' else {}
            result=runtime.executor.execute(ToolCall('probe','mcp__demo__query',args),runtime.context)
            assert result.status==ToolStatus.ERROR, result
            assert len(Connection.instances[0].calls)==(0 if phase=='input' else 1)
            if phase=='input':assert '超时' in result.output
            else:
                assert result.metadata['result_validation_error']
                assert result.metadata['schema_validation_timeout']
                assert not result.metadata['execution_unknown']
            follow=runtime.executor.execute(ToolCall('next','write_file',{'path':'next.txt','content':'usable'}),runtime.context)
            assert follow.status==ToolStatus.SUCCESS,follow
        finally:runtime.close()
    ''')
    try:
        result = subprocess.run([sys.executable, '-c', script, str(tmp_path), phase,
                                 str(Path(__file__).parent)], capture_output=True, text=True, timeout=6)
    except subprocess.TimeoutExpired:
        pytest.fail(f'{phase} Schema validation blocked Runtime; isolated child terminated')
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / 'next.txt').read_text() == 'usable'


@pytest.mark.parametrize('phase', ['input', 'output'])
def test_cancel_validation_reaps_worker_without_replaying_remote(
        make_runtime, tmp_path, monkeypatch, phase):
    import json
    import threading
    from conftest import ScriptedModelClient
    from test_mcp import FakeConnection
    from mini_claude_code.core.loop import TurnOutcome
    from mini_claude_code.core.messages import ToolCall, ToolResult
    from mini_claude_code.core.model import ModelResponse, StopReason
    from mini_claude_code.mcp import schema_validation

    pattern = {'type': 'object', 'properties': {'value': {'type': 'string', 'pattern': '^(a+)+$'}}}
    definition = {'name': 'query', 'inputSchema': pattern if phase == 'input' else {'type': 'object'}}
    if phase == 'output':
        definition['outputSchema'] = pattern
    class Connection(FakeConnection):
        instances = []
        catalog = [definition]
        response = {'content': [], 'structuredContent': {'value': 'a' * 28 + '!'}}
    path = tmp_path / 'mcp.json'
    path.write_text(json.dumps({'schema_version': 1, 'servers': {'demo': {
        'command': sys.executable, 'tools': {'query': {'policy': 'allow', 'effect': 'write'}}}}}))
    args = {'value': 'a' * 28 + '!'} if phase == 'input' else {}
    client = ScriptedModelClient(ModelResponse([ToolCall('probe', 'mcp__demo__query', args)], StopReason.TOOL_CALL))
    runtime = make_runtime(client, mcp_config=str(path), mcp_connection_factory=Connection)
    runtime.mcp.connect('demo')
    real_popen = schema_validation.subprocess.Popen
    workers = []
    timers = []
    def spawn(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        workers.append(process)
        if len(workers) == (1 if phase == 'input' else 2):
            timer = threading.Timer(.3, runtime.cancellation.cancel)
            timers.append(timer)
            timer.start()
        return process
    with monkeypatch.context() as patch:
        patch.setattr(schema_validation.subprocess, 'Popen', spawn)
        outcome = runtime.run('Cancellation fixture')
    for timer in timers:
        timer.join(2)
    assert outcome == TurnOutcome.CANCELLED
    assert workers and all(p.poll() is not None for p in workers)
    assert all(p.stdin.closed and p.stdout.closed for p in workers)
    assert len(Connection.instances[0].calls) == (0 if phase == 'input' else 1)
    assert len(client.requests) == 1
    assert not runtime.cleanup_uncertain and not runtime.mcp.uncertain
    result = next(b for m in runtime.state.messages for b in m.content if isinstance(b, ToolResult))
    assert not result.metadata.get('execution_unknown')
    if phase == 'output':
        assert result.metadata['validation_cancelled'] and result.metadata['result_validation_error']
    else:
        assert result.status.value == 'cancelled' and not result.metadata['executed']


def test_validation_can_cancel_from_a_teammate_thread(monkeypatch):
    import threading
    from mini_claude_code.core.cancellation import CancellationToken, Cancelled
    from mini_claude_code.mcp.adapter import schema_validator, validate
    from mini_claude_code.mcp import schema_validation
    validator = schema_validator({'type': 'object', 'properties': {
        'value': {'type': 'string', 'pattern': '^(a+)+$'}}})
    token = CancellationToken()
    spawned = threading.Event()
    workers, errors = [], []
    real_popen = schema_validation.subprocess.Popen
    def spawn(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        workers.append(process)
        spawned.set()
        return process
    def run():
        try:
            validate(validator, {'value': 'a' * 28 + '!'}, cancellation=token)
        except BaseException as exc:
            errors.append(exc)
    monkeypatch.setattr(schema_validation.subprocess, 'Popen', spawn)
    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert spawned.wait(3)
    finally:
        token.cancel()
        thread.join(3)
    assert not thread.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], Cancelled)
    assert all(p.poll() is not None for p in workers)


def test_regular_schema_constraints_are_preserved():
    from mini_claude_code.mcp.adapter import schema_validator, validate
    from mini_claude_code.mcp.config import MCPError
    schema = {'type': 'object', 'properties': {
        'value': {'type': 'string', 'pattern': '^a+$'}, 'n': {'type': 'integer', 'minimum': 2}},
        'required': ['value', 'n'], 'additionalProperties': False}
    validator = schema_validator(schema)
    arguments = {'value': 'aaaa', 'n': 2}
    assert validate(validator, arguments) == arguments
    for invalid in ({'value': 'bbb', 'n': 2}, {'value': 'a', 'n': 1},
                    {'value': 'a'}, {'value': 'a', 'n': 2, 'extra': True}):
        with pytest.raises(MCPError):
            validate(validator, invalid)


def test_worker_crash_fails_closed_and_is_reaped(monkeypatch):
    from mini_claude_code.mcp.adapter import schema_validator, validate
    from mini_claude_code.mcp.config import MCPError
    from mini_claude_code.mcp import schema_validation
    validator = schema_validator({'type': 'object'})
    real_popen = schema_validation.subprocess.Popen
    workers = []
    def crash(command, **kwargs):
        process = real_popen([sys.executable, '-c', 'raise SystemExit(7)'], **kwargs)
        workers.append(process)
        return process
    monkeypatch.setattr(schema_validation.subprocess, 'Popen', crash)
    with pytest.raises(MCPError):
        validate(validator, {})
    assert workers and workers[0].poll() == 7
    assert workers[0].stdin.closed and workers[0].stdout.closed


def test_timeout_reaps_worker_and_next_validation_still_works(monkeypatch):
    from mini_claude_code.mcp.adapter import schema_validator, validate
    from mini_claude_code.mcp import schema_validation
    validator = schema_validator({'type': 'object', 'properties': {
        'value': {'type': 'string', 'pattern': '^(a+)+$'}}})
    real_popen = schema_validation.subprocess.Popen
    workers = []
    def spawn(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        workers.append(process)
        return process
    monkeypatch.setattr(schema_validation.subprocess, 'Popen', spawn)
    with pytest.raises(schema_validation.SchemaValidationTimeout):
        validate(validator, {'value': 'a' * 28 + '!'})
    assert workers[0].poll() is not None
    assert workers[0].stdin.closed and workers[0].stdout.closed
    assert validate(validator, {'value': 'aaaa'}) == {'value': 'aaaa'}


def test_keyboard_interrupt_reaps_validation_worker(monkeypatch):
    from mini_claude_code.mcp.adapter import schema_validator, validate
    from mini_claude_code.mcp import schema_validation
    validator = schema_validator({'type': 'object'})
    real_popen = schema_validation.subprocess.Popen
    workers = []
    def spawn(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        workers.append(process)
        communicate = process.communicate
        first = True
        def interrupt(*args, **kwargs):
            nonlocal first
            if first:
                first = False
                raise KeyboardInterrupt()
            return communicate(*args, **kwargs)
        process.communicate = interrupt
        return process
    monkeypatch.setattr(schema_validation.subprocess, 'Popen', spawn)
    with pytest.raises(KeyboardInterrupt):
        validate(validator, {})
    assert workers[0].poll() is not None
    assert workers[0].stdin.closed and workers[0].stdout.closed


def test_worker_does_not_import_workspace_shadow_packages(tmp_path, monkeypatch):
    from mini_claude_code.mcp.adapter import schema_validator, validate
    from mini_claude_code.mcp.config import MCPError
    fake_package = tmp_path / 'mini_claude_code'
    fake_package.mkdir()
    (fake_package / '__init__.py').write_text("from pathlib import Path\nPath('shadow-imported').write_text('ran')\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('PYTHONPATH', str(tmp_path))
    validator = schema_validator({'type': 'object', 'required': ['needed']})
    with pytest.raises(MCPError):
        validate(validator, {})
    assert not (tmp_path / 'shadow-imported').exists()
