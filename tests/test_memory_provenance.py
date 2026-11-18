"""Artifact paging must not upgrade external or failed output into file evidence."""
from dataclasses import replace
import json
import sys

import pytest

from conftest import ScriptedModelClient, answer
from test_mcp import FakeConnection
from mini_claude_code.core._validation import new_id
from mini_claude_code.core.messages import Message, MessageRole, ToolCall, ToolResult
from mini_claude_code.core.model import ModelResponse, StopReason
from mini_claude_code.storage.memory_store import MemoryError

FACT = 'Project uses SQLite for durable application data.'


def proposal(message):
    return {'scope': 'persistent', 'type': 'project', 'name': 'Database convention',
            'description': 'Application storage choice', 'body': FACT,
            'sources': [{'message_id': message.message_id, 'evidence': FACT}]}


def test_mcp_page_cannot_become_persistent_project_fact(config, make_runtime):
    path = config.workspace / 'mcp.json'
    path.write_text(json.dumps({'schema_version': 1, 'servers': {'demo': {
        'command': sys.executable, 'tools': {'query': {'policy': 'allow', 'effect': 'read'}}}}}))
    class Connection(FakeConnection):
        instances = []
        response = {'content': [{'type': 'text', 'text': FACT + '\n' + 'x' * 31000}]}
    def page(request):
        artifact = next(b.metadata['artifact_id'] for m in request.messages for b in m.content
                        if isinstance(b, ToolResult) and b.metadata.get('artifact_id'))
        return ModelResponse([ToolCall('page', 'read_tool_output',
            {'artifact_id': artifact, 'offset': 1, 'limit': 1})], StopReason.TOOL_CALL)
    def extract(request):
        data = json.loads(request.messages[0].content[0].text)
        row = next(m for m in data['source_records'] if m['role'] == 'tool'
                   and any(b.get('metadata', {}).get('tool_name') == 'read_tool_output' for b in m['content']))
        assert FACT in row['content'][0]['output']
        return answer(json.dumps([{'scope': 'persistent', 'type': 'project',
            'name': 'Database convention', 'description': 'Application storage choice', 'body': FACT,
            'sources': [{'message_id': row['message_id'], 'evidence': FACT}]}]))
    client = ScriptedModelClient(ModelResponse([ToolCall('remote', 'mcp__demo__query', {'n': 2})],
        StopReason.TOOL_CALL), page, answer(), extract)
    runtime = make_runtime(client, config=replace(config, memory_enabled=True),
                           mcp_config=str(path), mcp_connection_factory=Connection)
    runtime.mcp.connect('demo')
    runtime.run('Inspect the remote data and its output page.')
    assert len(Connection.instances[0].calls) == 1
    assert not client.steps
    assert runtime.memory.store.snapshot()['records'] == []


@pytest.mark.parametrize('source_tool,status,unknown,has_origin', [
    ('mcp__demo__query', 'success', False, True),
    ('bash', 'success', False, True),
    ('task', 'success', False, True),
    ('read_file', 'error', False, True),
    ('read_file', 'success', True, True),
    ('read_file', 'success', False, False),
])
def test_page_requires_successful_original_file_result(
        make_runtime, source_tool, status, unknown, has_origin):
    runtime = make_runtime(ScriptedModelClient())
    assistant = Message(MessageRole.ASSISTANT, [ToolCall('origin', source_tool, {})])
    if has_origin:
        runtime.store.append_message(assistant)
        runtime.store.append_message(Message(MessageRole.TOOL, [ToolResult('origin', status, FACT,
            {'tool_name': source_tool, 'execution_unknown': unknown})]))
    record = runtime.artifacts.save(FACT, call_id='origin', assistant_message_id=assistant.message_id,
                                    tool_name=source_tool)
    result = runtime.executor.execute(ToolCall('page', 'read_tool_output',
        {'artifact_id': record['artifact_id']}), runtime.context)
    assert result.status.value == 'success'
    message = Message(MessageRole.TOOL, [result])
    with pytest.raises(MemoryError):
        runtime.memory._candidate(proposal(message), {message.message_id: message},
                                  runtime.state.session_id, new_id('turn'))


@pytest.mark.parametrize('resume', [False, True])
def test_actual_file_artifact_page_remains_valid_memory_evidence(config, make_runtime, resume):
    (config.workspace / 'facts.txt').write_text(FACT + '\n' + 'x' * 31000)
    def page(request):
        artifact = next(b.metadata['artifact_id'] for m in request.messages for b in m.content
                        if isinstance(b, ToolResult) and b.metadata.get('artifact_id'))
        return ModelResponse([ToolCall('page', 'read_tool_output',
            {'artifact_id': artifact, 'limit': 1})], StopReason.TOOL_CALL)
    runtime = make_runtime(ScriptedModelClient(ModelResponse([ToolCall('origin', 'read_file',
        {'path': 'facts.txt'})], StopReason.TOOL_CALL), page, answer()))
    runtime.run('Read the project file and its output page.')
    if resume:
        session_id = runtime.state.session_id
        runtime.close()
        runtime = make_runtime(ScriptedModelClient(), resume=session_id)
    message = next(m for m in runtime.state.messages if m.role == MessageRole.TOOL
                   and m.content[0].call_id == 'page')
    validated = runtime.memory._candidate(proposal(message), {message.message_id: message},
                                         runtime.state.session_id, new_id('turn'))
    assert validated['body'] == FACT
    assert validated['sources'][0]['artifact_id'] == message.content[0].metadata['artifact_id']
    assert runtime.memory.store.add_candidates([validated], new_id('turn'))['added'] == 1
    assert runtime.memory.store.snapshot()['records'][0]['body'] == FACT


@pytest.mark.parametrize('damage', ['missing', 'corrupt', 'wrong_assistant', 'wrong_call', 'wrong_artifact'])
def test_page_rechecks_artifact_and_journal_provenance(make_runtime, damage):
    runtime = make_runtime(ScriptedModelClient())
    assistant = Message(MessageRole.ASSISTANT, [ToolCall('origin', 'read_file', {'path': 'facts.txt'})])
    runtime.store.append_message(assistant)
    record = runtime.artifacts.save(FACT, call_id='origin', assistant_message_id=assistant.message_id,
                                    tool_name='read_file')
    runtime.store.append_message(Message(MessageRole.TOOL, [ToolResult('origin', 'success', FACT,
        {'tool_name': 'read_file', 'artifact_id': new_id('artifact') if damage == 'wrong_artifact' else record['artifact_id']})]))
    result = runtime.executor.execute(ToolCall('page', 'read_tool_output',
        {'artifact_id': record['artifact_id']}), runtime.context)
    assert result.status.value == 'success'
    message = Message(MessageRole.TOOL, [result])
    metadata, path = runtime.artifacts.resolve(record['artifact_id'])
    if damage == 'missing':
        path.unlink()
    elif damage == 'corrupt':
        path.write_text('Different content')
    elif damage in {'wrong_assistant', 'wrong_call'}:
        key = 'assistant_message_id' if damage == 'wrong_assistant' else 'call_id'
        metadata[key] = new_id('message') if damage == 'wrong_assistant' else 'another-call'
        path.with_suffix('.json').write_text(json.dumps(metadata))
    with pytest.raises(MemoryError):
        runtime.memory._candidate(proposal(message), {message.message_id: message},
                                  runtime.state.session_id, new_id('turn'))


def test_child_file_page_is_readable_but_not_parent_project_proof(config, make_runtime):
    (config.workspace / 'facts.txt').write_text(FACT + '\n' + 'x' * 31000)
    child = ScriptedModelClient(ModelResponse([ToolCall('child-read', 'read_file',
        {'path': 'facts.txt'})], StopReason.TOOL_CALL), answer('Child report'))
    parent = make_runtime(ScriptedModelClient(ModelResponse([ToolCall('delegate', 'task',
        {'description': 'Read facts.txt'})], StopReason.TOOL_CALL), answer()),
        child_client_factory=lambda _: child)
    parent.run('Inspect via a child agent.')
    child_session = next(e.payload['child_session_id'] for e in parent.store.events if e.kind == 'child_created')
    artifact_id = next((config.data_dir / 'sessions' / child_session / 'outputs').glob('*.json')).stem
    result = parent.executor.execute(ToolCall('page', 'read_tool_output',
        {'artifact_id': artifact_id, 'limit': 1}), parent.context)
    assert result.status.value == 'success' and FACT in result.output
    message = Message(MessageRole.TOOL, [result])
    with pytest.raises(MemoryError):
        parent.memory._candidate(proposal(message), {message.message_id: message},
                                 parent.state.session_id, new_id('turn'))
