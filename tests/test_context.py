"""Output retention, whole exchanges, bounded compaction and recovery."""

from copy import deepcopy
from dataclasses import replace
import json
import sys

import pytest

from conftest import ScriptedModelClient,answer
from mini_claude_code.core.messages import Message,MessageRole,TextBlock,ToolCall,ToolResult
from mini_claude_code.core.model import ModelResponse,StopReason
from mini_claude_code.core.errors import ModelError,StorageError,ContractError
from mini_claude_code.core.cancellation import Cancelled,CancellationToken
from mini_claude_code.core.loop import validate_history
from mini_claude_code.agents.factory import AgentSessionFactory
from mini_claude_code.providers.recovery import RequestRecovery
from mini_claude_code.context.budget import chars
from mini_claude_code.context.compaction import fragments


def calls(*items):return ModelResponse(list(items),StopReason.TOOL_CALL)
def results(rt):return [b for m in rt.state.messages for b in m.content if isinstance(b,ToolResult)]


class ContextModel(ScriptedModelClient):
    def __init__(self,*steps):
        super().__init__(*steps);self.summaries=[];self.error=None
    def generate(self,req):
        if req.tools:
            return super().generate(req) if self.steps else self._record(req)
        self.summaries.append(deepcopy(req))
        if self.error:raise self.error
        return answer('Factual summary: preserve user constraints, read-only behavior, verified facts and unfinished work.')
    def _record(self,req):
        self.requests.append(deepcopy(req));return answer()


def add_history(rt,count=6):
    for index in range(count):
        rt.store.append_message(Message(MessageRole.USER,[TextBlock(f'Historical source {index}: '+'background '*100)]))
        rt.store.append_message(Message(MessageRole.ASSISTANT,[TextBlock('Prior response.')]))


def test_large_output_paged_and_missing_never_replayed(make_runtime,config):
    (config.workspace/'large').write_text(''.join(f'row {i}: '+'x'*40+'\n' for i in range(2000)))
    client=ScriptedModelClient(calls(ToolCall('read','read_file',{'path':'large','limit':2000})),answer())
    rt=make_runtime(client);rt.run('read')
    original=results(rt)[0];artifact=original.metadata['artifact_id']
    assert len(original.output)<=2000 and not original.metadata['truncated_at_capture']
    page=rt.executor.execute(ToolCall('page','read_tool_output',{'artifact_id':artifact,'offset':1999,'limit':1}),rt.context)
    assert 'row 1999:' in page.output
    record,path=rt.artifacts.resolve(artifact)
    assert record['assistant_message_id']==rt.state.messages[1].message_id and record['call_id']=='read'
    path.unlink()
    missing=rt.executor.execute(ToolCall('missing','read_tool_output',{'artifact_id':artifact}),rt.context)
    assert missing.status.value=='error' and len(client.requests)==2


def test_unicode_long_line_pages_are_complete_bounded_and_nonrecursive(make_runtime):
    rt=make_runtime(ScriptedModelClient())
    raw='甲乙🙂'*60000+'\r\nlast\n'
    artifact=rt.artifacts.save(raw,call_id='source',assistant_message_id=None,tool_name='fixture')['artifact_id']
    offset=char_offset=0;pieces=[]
    for count in range(20):
        result=rt.executor.execute(ToolCall(f'page-{count}','read_tool_output',
            {'artifact_id':artifact,'offset':offset,'char_offset':char_offset,'limit':2000}),rt.context)
        assert result.status.value=='success' and len(result.output)<=50000
        body=result.output.split('\n',1)[1].rsplit('\n[has_more=',1)[0]
        for line in body.splitlines(keepends=True):
            pieces.append(line.split(': ',1)[1] if line.startswith(('1','2: ')) else line)
        if not result.metadata['has_more']:break
        newer=result.metadata['next_offset'],result.metadata['next_char_offset']
        assert newer>(offset,char_offset)
        offset,char_offset=newer
    else:raise AssertionError('Paging did not terminate')
    assert ''.join(pieces)==raw and len(list(rt.artifacts.root.iterdir()))==2


def test_capture_and_quota_limits(make_runtime,monkeypatch):
    monkeypatch.setattr('mini_claude_code.storage.artifacts.SINGLE_LIMIT',4096)
    command=f'{sys.executable} -c "print(chr(120)*100000)"'
    client=ScriptedModelClient(calls(ToolCall('shell','bash',{'command':command})),answer())
    rt=make_runtime(client);rt.run('capture')
    result=results(rt)[0]
    assert result.metadata['truncated_at_capture'] and result.metadata['bytes_stored']<=4096
    assert result.status.value=='success'
    monkeypatch.setattr('mini_claude_code.storage.artifacts.SESSION_LIMIT',1)
    with pytest.raises(StorageError):
        rt.artifacts.save('new',call_id='quota',assistant_message_id=None,tool_name='fixture')


def test_parent_child_artifact_authorization(make_runtime):
    rt=make_runtime(ScriptedModelClient())
    parent=rt.artifacts.save('parent',call_id='p',assistant_message_id=None,tool_name='fixture')['artifact_id']
    rt.store.append_message(Message(MessageRole.ASSISTANT,[ToolCall('linked','task',{'description':'read'})]))
    factory=AgentSessionFactory(rt,lambda _:ScriptedModelClient())
    with factory.create('linked','analyze') as child:
        own=child.artifacts.save('child',call_id='c',assistant_message_id=None,tool_name='fixture')['artifact_id']
        with pytest.raises(ValueError):child.artifacts.resolve(parent)
    assert rt.artifacts.resolve(own)[0]['artifact_id']==own
    with factory.create('other','analyze') as sibling:
        with pytest.raises(ValueError):sibling.artifacts.resolve(own)


def test_compact_preserves_canonical_and_restore_appends_new_messages(make_runtime,config):
    client=ContextModel();cfg=replace(config,context_budget_chars=22000)
    rt=make_runtime(client,config=cfg);add_history(rt)
    before=[m.to_dict() for m in rt.state.messages]
    view=rt.compact('Preserve facts')
    assert view.chars<=22000 and view.auxiliary_calls>0
    assert [m.to_dict() for m in rt.state.messages]==before
    validate_history(view.messages)
    sid=rt.state.session_id;rt.close()
    new=ContextModel()
    restored=make_runtime(new,config=cfg,resume=sid)
    assert restored.context_manager.checkpoint and not new.requests and not new.summaries
    restored.run('CURRENT_REQUEST must stay complete')
    assert 'CURRENT_REQUEST must stay complete' in json.dumps([m.to_dict() for m in new.requests[-1].messages])
    assert [m.to_dict() for m in restored.state.messages[:len(before)]]==before


@pytest.mark.parametrize('error',[ModelError('summary failed'),Cancelled(),KeyboardInterrupt()])
def test_summary_failure_cancel_never_publishes_partial_checkpoint(make_runtime,error):
    client=ContextModel();rt=make_runtime(client);add_history(rt)
    rt.compact();old=deepcopy(rt.context_manager.checkpoint)
    add_history(rt,2);before=[m.to_dict() for m in rt.state.messages]
    client.error=error
    with pytest.raises(type(error)):rt.compact()
    assert rt.context_manager.checkpoint==old
    assert [m.to_dict() for m in rt.state.messages]==before


def test_multitool_error_pairing_and_current_request_protected(make_runtime,config):
    client=ContextModel(calls(ToolCall('one','read_file',{'path':'missing'}),ToolCall('two','glob',{'pattern':'*'})),answer())
    rt=make_runtime(client,config=replace(config,context_budget_chars=18000))
    rt.run('CURRENT_REQUEST exact user text')
    add_history(rt)
    # Current request is the most recent real user message.
    current=rt.state.messages[-2]
    view=rt.compact()
    validate_history(view.messages)
    assert current.to_dict() in [m.to_dict() for m in view.messages]
    exchanges=[group for group in fragments(view.messages) if any(isinstance(b,ToolCall) for b in group[0].content)]
    assert len(exchanges)==1
    kept=[b for m in exchanges[0] for b in m.content if isinstance(b,ToolResult)]
    assert {b.call_id for b in kept}=={'one','two'} and kept[0].status.value=='error'


def test_snip_archives_complete_sources_and_keeps_user_index(make_runtime):
    client=ContextModel();rt=make_runtime(client);add_history(rt,30)
    view=rt.context_manager.prepare(rt.loop.system.build(rt.registry.specs()),rt.registry.specs(),
                                    rt.config.model,CancellationToken())
    assert len(rt.state.messages)==60 and len(view.messages)<=31
    assert '历史原文索引' in view.messages[0].content[0].text
    assert not client.summaries
    validate_history(view.messages)


def test_corrupt_checkpoint_warns_without_model_on_resume(make_runtime):
    client=ContextModel();rt=make_runtime(client);add_history(rt);rt.compact()
    point=rt.context_manager.checkpoint
    path=rt.store.path/f'context/checkpoints/{point["context_id"]}.json'
    sid=rt.state.session_id;rt.close();path.write_text('bad checkpoint')
    notices=[];new=ContextModel()
    resumed=make_runtime(new,resume=sid,display=lambda kind,value:notices.append(value))
    assert resumed.context_manager.checkpoint is None and notices
    assert not new.requests and not new.summaries and len(resumed.state.messages)==12


def test_reactive_compact_once_with_no_tool_replay(make_runtime):
    client=ContextModel();rt=make_runtime(client);add_history(rt)
    client.steps=[ModelError('context too long',context_too_long=True),answer()]
    rt.run('CURRENT_REQUEST continue')
    assert len(client.requests)==2
    assert len([event for event in rt.store.events if event.kind=='reactive_compact'])==1
    assert not results(rt)


@pytest.mark.parametrize('errors,expected_main,expected_compact',[
    ([ModelError('long',context_too_long=True,retryable=True)]*2,2,1),
    ([ModelError('network',retryable=True)]*2+[ModelError('long',context_too_long=True)],3,0),
])
def test_reactive_and_network_share_attempt_limit(config,errors,expected_main,expected_compact):
    from mini_claude_code.core.model import ModelRequest
    client=ScriptedModelClient(*errors);compact=[]
    recovery=RequestRecovery(client,config,CancellationToken(),stream=False,wait=lambda _:None)
    with pytest.raises(ModelError):
        recovery.generate(ModelRequest('system',[],[],config.model,8000),reactive=lambda r:(compact.append(True) or r))
    assert len(client.requests)==expected_main and len(compact)==expected_compact


def test_system_and_tools_count_toward_budget(make_runtime,config):
    rt=make_runtime(ContextModel(),config=replace(config,context_budget_chars=100))
    with pytest.raises(ContractError,match='占满字符预算'):rt.run('current user')
    assert not rt.client.requests and not rt.client.summaries
