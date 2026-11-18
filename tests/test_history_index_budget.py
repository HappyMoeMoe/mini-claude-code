"""Rolling compaction bounds generated history, while retaining source journals."""
from dataclasses import replace
import json
import re

import pytest

from test_context import ContextModel
from mini_claude_code.core.loop import TurnOutcome, validate_history
from mini_claude_code.core.messages import Message, TextBlock
from mini_claude_code.core.cancellation import CancellationToken


def add_rounds(runtime, start, count):
    for index in range(start, start + count):
        runtime.store.append_message(Message('user', [TextBlock(f'Short round {index}: inspect only.')]))
        runtime.store.append_message(Message('assistant', [TextBlock('Inspected.')]))


@pytest.mark.parametrize('budget', [50_000, 22_000])
def test_five_hundred_rounds_keep_compacting_and_resume_with_a_short_request(config, make_runtime, budget):
    cfg = replace(config, context_budget_chars=budget)
    client = ContextModel()
    runtime = make_runtime(client, config=cfg)
    for batch in range(50):
        add_rounds(runtime, batch * 10, 10)
        path = runtime.store.path / 'messages.jsonl'
        before = path.read_bytes()
        view = runtime.compact()
        assert view.chars <= budget
        assert path.read_bytes() == before
        validate_history(view.messages)
    assert len(runtime.state.messages) == 1000
    original = [m.to_dict() for m in runtime.state.messages]
    session_id = runtime.state.session_id
    runtime.close()
    fresh = ContextModel()
    restored = make_runtime(fresh, config=cfg, resume=session_id)
    assert restored.context_manager.checkpoint is not None
    assert not fresh.requests and not fresh.summaries
    assert restored.run('CURRENT: one short new request') == TurnOutcome.COMPLETED
    assert len(fresh.requests) == 1
    assert [m.to_dict() for m in restored.state.messages[:1000]] == original
    assert any('CURRENT: one short new request' in block.text for msg in fresh.requests[0].messages
               for block in msg.content if isinstance(block, TextBlock))
    validate_history(restored.state.messages)


def test_index_accounts_for_escaped_unicode_and_keeps_recent_excerpts(make_runtime):
    client = ContextModel()
    runtime = make_runtime(client)
    for number in range(30):
        runtime.store.append_message(Message('user', [TextBlock(f'EXCERPT_{number:03d}: ' + '🙂\\\t"\n' * 40)]))
        runtime.store.append_message(Message('assistant', [TextBlock('Inspected.')]))
    path = runtime.store.path / 'messages.jsonl'
    before = path.read_bytes()
    manager = runtime.context_manager
    tools = runtime.registry.specs()
    view = manager.prepare(runtime.loop.system.build(tools), tools, runtime.config.model, CancellationToken())
    background = view.messages[0]
    text = background.content[0].text
    index = text.split('[HISTORY INDEX:', 1)[1].split('\n', 1)[1]
    budget = min(manager.limits.history_index_chars, manager.limits.budget_chars // 10)
    assert len(json.dumps(index, ensure_ascii=False)) <= budget
    assert int(re.search(r'omitted=(\d+)', text).group(1)) > 0
    archived = [m for m in runtime.state.messages if m.is_human and m.message_id not in view.source_message_ids]
    assert archived[-1].content[0].text[:12] in index
    assert 'EXCERPT_000:' not in index
    assert 'journal=messages.jsonl' in text and runtime.state.session_id in text
    assert not background.is_human and background.origin == {'kind': 'recovery'}
    assert not client.summaries and path.read_bytes() == before
    validate_history(view.messages)


def test_zero_excerpt_budget_retains_summary_current_input_and_original_history(make_runtime):
    runtime = make_runtime(ContextModel())
    add_rounds(runtime, 0, 6)
    current = runtime.state.messages[-2]
    path = runtime.store.path / 'messages.jsonl'
    before = path.read_bytes()
    runtime.context_manager.limits = replace(runtime.context_manager.limits, history_index_chars=0)
    view = runtime.compact()
    text = view.messages[0].content[0].text
    assert 'shown=0; omitted=5;' in text
    assert '[FACTUAL SUMMARY' in text and 'Factual summary:' in text
    assert current.to_dict() in [m.to_dict() for m in view.messages]
    assert path.read_bytes() == before
    validate_history(view.messages)


def test_checkpoint_without_new_budget_field_restores_without_auxiliary_calls(make_runtime):
    runtime = make_runtime(ContextModel())
    add_rounds(runtime, 0, 40)
    runtime.compact()
    checkpoint = runtime.context_manager.checkpoint
    path = runtime.store.path / 'context/checkpoints' / (checkpoint['context_id'] + '.json')
    data = json.loads(path.read_text())
    del data['budget_config']['history_index_chars']
    path.write_text(json.dumps(data, ensure_ascii=False))
    original = [m.to_dict() for m in runtime.state.messages]
    session_id = runtime.state.session_id
    runtime.close()
    client = ContextModel()
    restored = make_runtime(client, resume=session_id)
    assert restored.context_manager.checkpoint is not None
    assert not client.requests and not client.summaries
    assert restored.run('CURRENT: use the legacy checkpoint') == TurnOutcome.COMPLETED
    assert len(client.requests) == 1
    assert [m.to_dict() for m in restored.state.messages[:len(original)]] == original
