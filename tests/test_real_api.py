"""Live provider and agent acceptance tests; no model or HTTP replacements."""

from dataclasses import replace
import importlib.util
import json
from pathlib import Path
import re
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest

from mini_claude_code.config import configure_profile, load_config
from mini_claude_code.core.loop import TurnOutcome, validate_history
from mini_claude_code.core.messages import Message, TextBlock, ToolResult, ToolStatus
from mini_claude_code.core.model import ModelRequest, StopReason
from mini_claude_code.providers.deepseek import DeepSeekClient
from mini_claude_code.runtime import Runtime


pytestmark = pytest.mark.real_api
PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope='session')
def real_settings():
    settings = load_config(workspace=PROJECT_ROOT)
    errors = settings.model_readiness_errors()
    if errors:
        pytest.fail('真实 API 测试需要有效配置：' + ' '.join(errors)
                    + ' 仅运行离线回归时使用 -m "not real_api"。', pytrace=False)
    return settings


@pytest.fixture
def real_config(real_settings, tmp_path):
    return replace(real_settings, workspace=tmp_path, memory_enabled=False,
                   fallback_model=None, max_output_tokens=2048,
                   max_recovery_output_tokens=2048)


@pytest.fixture(scope='session')
def real_examples(real_settings):
    # Load exact repository paths so these work in both source and sdist checkouts.
    modules = {}
    for name in ('demo_real', 'demo_teams'):
        spec = importlib.util.spec_from_file_location(name, PROJECT_ROOT / 'examples' / f'{name}.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        modules[name] = module
    return SimpleNamespace(**modules)


def response_text(content):
    return ''.join(block.text for block in content if isinstance(block, TextBlock))


@pytest.mark.parametrize('stream', [False, True], ids=['messages', 'stream'])
def test_real_messages_api(real_config, stream):
    request = ModelRequest(
        system='Answer the arithmetic question briefly, without using tools.',
        messages=[Message('user', [TextBlock('What is 17 + 25?')])], tools=[],
        model=real_config.model, max_output_tokens=256)
    client = DeepSeekClient(real_config)
    try:
        if stream:
            events = list(client.stream(request))
            finals = [event.response for event in events if event.kind == 'final']
            assert len(finals) == 1
            response = finals[0]
            assert ''.join(event.text for event in events if event.kind == 'text') == response_text(response.content)
        else:
            response = client.generate(request)
        assert response.stop_reason == StopReason.NORMAL
        assert re.search(r'\b42\b', response_text(response.content))
        assert response.provider_request_id
        assert response.usage and response.usage.input_tokens > 0 and response.usage.output_tokens > 0
    finally:
        client.close()


def test_real_model_repairs_code_and_restores_session(real_examples, tmp_path):
    # The real model edits a broken function; fixed host unittest checks the result.
    real_examples.demo_real.scenario_a(tmp_path)


def test_real_model_delegates_to_subagent(real_examples, tmp_path):
    real_examples.demo_real.scenario_b(tmp_path)


def test_real_model_extracts_and_recalls_memory(real_examples, tmp_path):
    real_examples.demo_real.scenario_c(tmp_path)


def test_real_model_compacts_and_resumes(real_config):
    config = configure_profile(real_config, minimal=True)
    nonce = 'project-' + uuid4().hex
    with Runtime(config, interactive=False, stream=False, max_rounds=3) as runtime:
        assert runtime.run(f'The project identifier is {nonce}. Remember it for this conversation. '
                           'Reply briefly without calling tools.') == TurnOutcome.COMPLETED
        assert runtime.run('We will use Python for the project. Reply briefly without calling tools.') == TurnOutcome.COMPLETED
        journal = runtime.store.path / 'messages.jsonl'
        original = journal.read_bytes()
        view = runtime.compact('Preserve the exact project identifier.')
        assert view.auxiliary_calls > 0
        assert runtime.context_manager.checkpoint is not None
        assert nonce in runtime.context_manager.checkpoint['summary']
        assert journal.read_bytes() == original
        validate_history(view.messages)
        session_id = runtime.state.session_id
        original_messages = [message.to_dict() for message in runtime.state.messages]
    with Runtime(config, interactive=False, stream=False, resume=session_id, max_rounds=3) as restored:
        assert restored.context_manager.checkpoint is not None
        assert restored.run('What is the project identifier? Reply only with the identifier, '
                            'without calling tools.') == TurnOutcome.COMPLETED
        assert nonce in response_text(restored.state.messages[-1].content)
        assert [message.to_dict() for message in restored.state.messages[:len(original_messages)]] == original_messages
        validate_history(restored.state.messages)


def test_real_model_calls_local_mcp_tool(real_config):
    path = real_config.workspace / 'mcp.json'
    path.write_text(json.dumps({'schema_version': 1, 'servers': {'demo': {
        'command': sys.executable,
        'args': [str(PROJECT_ROOT / 'examples/mcp_server.py')],
        'tools': {'query': {'policy': 'allow', 'effect': 'read'}}}}}))
    nonce = 'mcp-check-' + uuid4().hex
    with Runtime(real_config, interactive=False, stream=False, max_rounds=4,
                 mcp_config=str(path)) as runtime:
        runtime.mcp.connect('demo')
        connection = runtime.mcp.entries['demo'].connection
        assert runtime.run(f'Call mcp__demo__query exactly once with text={nonce}. '
                           'Then report the returned echo. Do not call other tools.') == TurnOutcome.COMPLETED
        results = [block for message in runtime.state.messages for block in message.content
                   if isinstance(block, ToolResult) and block.metadata.get('tool_name') == 'mcp__demo__query']
        assert len(results) == 1
        assert results[0].status == ToolStatus.SUCCESS and nonce in results[0].output
        assert nonce in response_text(runtime.state.messages[-1].content)
        validate_history(runtime.state.messages)
    assert connection.confirmed and connection.process.returncode is not None
    assert not runtime.mcp.hub.thread.is_alive()


def test_real_model_team_uses_independent_worktrees(real_examples, monkeypatch):
    # This scenario checks both independent checkouts with fixed unittest cases
    # and verifies worker shutdown. Only CLI argument parsing is supplied here.
    monkeypatch.setattr(sys, 'argv', ['demo_teams.py', '--real'])
    real_examples.demo_teams.main()
