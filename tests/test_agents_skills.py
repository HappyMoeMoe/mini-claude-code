"""Session plans, on-demand knowledge and one-shot delegation boundaries."""

from dataclasses import replace
import json

import pytest

from conftest import ScriptedModelClient, answer
from mini_claude_code.core.loop import TurnOutcome
from mini_claude_code.core.messages import ToolCall,ToolResult,TextBlock,Message,MessageRole
from mini_claude_code.core.model import ModelResponse,StopReason
from mini_claude_code.core.errors import StorageError
from mini_claude_code.storage.session_store import SessionStore
from mini_claude_code.tasks.todo import normalize
from mini_claude_code.agents.factory import AgentSessionFactory
from mini_claude_code.storage.memory_store import MemoryStore
from test_memory import MemoryModel, seed


def calls(*items): return ModelResponse(list(items),StopReason.TOOL_CALL)
def results(runtime):
    return [b for m in runtime.state.messages for b in m.content if isinstance(b,ToolResult)]


@pytest.mark.parametrize('items',[
    [{'content':' ','status':'pending'}],
    [{'content':'x','status':'invalid'}],
    [{'content':'x','status':'in_progress'}]*2,
    [{'content':'x','status':'pending'}]*31,
    [{'content':'x','status':'pending','extra':True}],
])
def test_todo_validation(items):
    with pytest.raises(ValueError): normalize(items)


def test_todo_replacement_restore_and_uncertain_results(make_runtime):
    client=ScriptedModelClient(calls(ToolCall('todo','todo_write',{'todos':[{'content':' inspect ','status':'in_progress'}]})),answer())
    rt=make_runtime(client);rt.run('make plan')
    sid=rt.state.session_id
    assert rt.todos.current()[0].content=='inspect'
    rt.store.append_message(Message(MessageRole.ASSISTANT,[ToolCall('uncertain','todo_write',{'todos':[]})]))
    rt.store.append_message(Message(MessageRole.TOOL,[ToolResult('uncertain','success','unknown',
        {'tool_name':'todo_write','todos':[],'execution_unknown':True})]))
    assert rt.todos.current()[0].content=='inspect'
    rt.close()
    restored=make_runtime(ScriptedModelClient(),resume=sid)
    assert restored.todos.current()[0].content=='inspect'
    restored.client.steps=[calls(ToolCall('clear','todo_write',{'todos':[]})),answer()]
    restored.run('clear plan')
    assert restored.todos.current()==[]


def test_skill_body_loaded_only_on_demand(make_runtime,config):
    folder=config.workspace/'skills/review';folder.mkdir(parents=True)
    (folder/'SKILL.md').write_text('---\nname: review\ndescription: Inspect code carefully.\n---\nUNIQUE_SKILL_BODY read README first.\n')
    client=ScriptedModelClient(calls(ToolCall('load','load_skill',{'name':'review'})),answer())
    rt=make_runtime(client);rt.run('review code')
    assert 'Inspect code carefully' in client.requests[0].system
    assert 'UNIQUE_SKILL_BODY' not in client.requests[0].system
    assert 'UNIQUE_SKILL_BODY' not in json.dumps([m.to_dict() for m in client.requests[0].messages])
    assert 'UNIQUE_SKILL_BODY' in results(rt)[0].output
    assert 'UNIQUE_SKILL_BODY' in json.dumps([m.to_dict() for m in client.requests[1].messages])


def test_skill_duplicates_bad_yaml_and_external_symlink_disabled(make_runtime,config,tmp_path_factory):
    root=config.workspace/'skills';root.mkdir()
    for name in ('one','two'):
        path=root/name;path.mkdir()
        (path/'SKILL.md').write_text('---\nname: duplicate\ndescription: duplicated.\n---\nbody')
    bad=root/'bad';bad.mkdir();(bad/'SKILL.md').write_text('---\nname: [\n---\nbody')
    outside=tmp_path_factory.mktemp('external-skill')
    (outside/'SKILL.md').write_text('---\nname: outside\ndescription: external.\n---\nbody')
    (root/'external').symlink_to(outside,target_is_directory=True)
    rt=make_runtime(ScriptedModelClient(answer()));rt.run('inspect')
    assert not rt.skills.catalog.entries and len(rt.skills.catalog.warnings)>=3


def test_child_isolated_readonly_nested_denied_and_parent_continues(make_runtime):
    child=ScriptedModelClient(calls(ToolCall('write','write_file',{'path':'forbidden','content':'x'}),
                                    ToolCall('nested','task',{'description':'nested'})),answer('child report'))
    parent=ScriptedModelClient(calls(ToolCall('task','task',{'description':'Analyze only this scoped task'})),answer('parent complete'))
    rt=make_runtime(parent,child_client_factory=lambda _:child)
    rt.run('PARENT_PRIVATE_CONTEXT')
    assert rt.state.messages[-1].content[0].text=='parent complete'
    assert 'PARENT_PRIVATE_CONTEXT' not in json.dumps([m.to_dict() for m in child.requests[0].messages])
    assert all(tool.name not in {'write_file','bash','task','todo_write'} for tool in child.requests[0].tools)
    child_id=results(rt)[0].metadata['child_session_id']
    logs=[json.loads(line) for line in (rt.store.path.parent/child_id/'messages.jsonl').read_text().splitlines()]
    child_results=[b for m in logs for b in m['content'] if b['type']=='tool_result']
    assert all(b['status']=='denied' for b in child_results)
    assert not (rt.config.workspace/'forbidden').exists()
    assert len(SessionStore.list_sessions(rt.config.workspace))==1
    assert len(SessionStore.list_sessions(rt.config.workspace,include_children=True))==2
    with pytest.raises(StorageError):SessionStore.resume(rt.config.workspace,child_id)


def test_edit_child_shell_still_requires_approval(make_runtime,config):
    approvals=[]
    def approve(call,args,ctx):
        approvals.append(ctx.agent_id);return False
    child=ScriptedModelClient(calls(ToolCall('shell','bash',{'command':'touch denied'})),answer())
    parent=ScriptedModelClient(calls(ToolCall('task','task',{'description':'check','mode':'edit'})),answer())
    rt=make_runtime(parent,approve=approve,child_client_factory=lambda _:child)
    rt.run('delegate')
    assert len(approvals)==1 and approvals[0]!=rt.state.identity.agent_id
    assert not (config.workspace/'denied').exists()


def test_child_cancel_propagates_without_parent_continuation(make_runtime):
    child=ScriptedModelClient(KeyboardInterrupt())
    parent=ScriptedModelClient(calls(ToolCall('task','task',{'description':'analyze'})))
    rt=make_runtime(parent,child_client_factory=lambda _:child)
    assert rt.run('delegate')==TurnOutcome.CANCELLED
    assert len(parent.requests)==1 and results(rt)[0].status.value=='cancelled'


def test_child_storage_failure_is_fatal(make_runtime,monkeypatch):
    child=ScriptedModelClient(answer())
    parent=ScriptedModelClient(calls(ToolCall('task','task',{'description':'analyze'})))
    rt=make_runtime(parent,child_client_factory=lambda _:child)
    original=SessionStore.append_message
    def fail(store,message):
        if store.state.identity.role.value=='subagent':raise StorageError('child disk failure')
        return original(store,message)
    monkeypatch.setattr(SessionStore,'append_message',fail)
    with pytest.raises(StorageError):rt.run('delegate')
    assert not results(rt) and len(parent.requests)==1


def test_child_limit_and_dispatch_limit(make_runtime,monkeypatch):
    parent=ScriptedModelClient(calls(*[ToolCall(f'task-{i}','task',{'description':'analyze'}) for i in range(6)]),answer())
    children=[]
    def factory(_):
        child=ScriptedModelClient(calls(ToolCall('glob','glob',{'pattern':'*'})))
        children.append(child);return child
    original=AgentSessionFactory.create
    def short(self,*args):
        runtime=original(self,*args);runtime.loop.max_rounds=1;return runtime
    monkeypatch.setattr(AgentSessionFactory,'create',short)
    rt=make_runtime(parent,child_client_factory=factory);rt.run('dispatch')
    assert len(children)==5 and len(results(rt))==6
    assert all(r.status.value=='error' and r.metadata['outcome']=='limit_reached' for r in results(rt))


def test_child_memory_is_parent_selected_readonly_subset(make_runtime,config):
    record=seed(MemoryStore(config.workspace))[0]
    child=MemoryModel(answer('Python testing report'))
    parent=MemoryModel(calls(ToolCall('task','task',{'description':'检查 Python pytest 测试约定'})),answer(),
                       selector=[record['memory_id']])
    cfg=replace(config,memory_enabled=True)
    rt=make_runtime(parent,config=cfg,child_client_factory=lambda _:child)
    rt.run('检查 Python pytest 测试约定')
    assert record['memory_id'] in child.requests[0].system
    assert not child.aux
    assert len([x for x in parent.aux if x[0]=='select'])==1
