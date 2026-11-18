"""MCP host authority, snapshot boundaries and real stdio lifecycle."""
from dataclasses import replace
import json
from pathlib import Path
import sys
import threading
import time

import pytest

from conftest import ScriptedModelClient,answer
from mini_claude_code.core.messages import ToolCall,ToolResult,ToolStatus
from mini_claude_code.core.model import ModelResponse,StopReason
from mini_claude_code.core.cancellation import CancellationToken,Cancelled
from mini_claude_code.core.errors import ConfigurationError,StorageError
from mini_claude_code.mcp.config import load_mcp_config,MCPError
from mini_claude_code.mcp.adapter import schema_validator,validate,normalize,Binding
from mini_claude_code.mcp.client import Capacity
from mini_claude_code.runtime import Runtime


def calls(*items):return ModelResponse(list(items),StopReason.TOOL_CALL)
def call(id,tool_name,**args):return ToolCall(id,tool_name,args)
def results(runtime):return [b for m in runtime.state.messages for b in m.content if isinstance(b,ToolResult)]
def tool(name='query',**extra):return {'name':name,'inputSchema':{'type':'object','properties':{'n':{'type':'integer','minimum':2}},'required':['n']},**extra}


class FakeConnection:
    catalog=[tool()]
    pages=None
    fail=None
    response={'content':[{'type':'text','text':'external result'}]}
    instances=[]
    def __init__(self,hub,config,cwd,wake):
        self.hub,self.config,self.cwd,self.wake=hub,config,cwd,wake
        self.state='disconnected';self.confirmed=True;self.dirty=threading.Event();self.version='2025-11-25'
        self.calls=[];self.closed=False;self.slot=False;self.instances.append(self)
    def open(self,token=None):
        self.hub.capacity.acquire();self.slot=True;self.state='ready'
    def operation(self,kind,args,token=None,trace=None,**kw):
        if kind=='list':
            if self.pages is not None:return self.pages[args]
            return {'tools':self.catalog}
        self.calls.append(args);trace.update(sent=True,request_id=len(self.calls)+90)
        if self.fail:raise self.fail
        return self.response
    def close(self):
        self.closed=True;self.state='disconnected'
        if self.slot:self.hub.capacity.release();self.slot=False
        return self.confirmed


@pytest.fixture
def setup_mcp(tmp_path):
    class Connection(FakeConnection):instances=[]
    def setup(*,tools=None,servers=None,**fields):
        row={'command':sys.executable,'tools':tools if tools is not None else {'query':{'policy':'allow','effect':'read','roles':['lead','subagent','teammate']}},**fields}
        path=tmp_path/'mcp.json';path.write_text(json.dumps({'schema_version':1,'servers':servers or {'demo':row}}))
        return str(path),Connection
    return setup


@pytest.mark.parametrize('change',[
    {'schema_version':2}, {'surprise':True}, {'servers':{'bad.name':{'command':sys.executable}}},
    {'servers':{'demo':{'command':'python'}}}, {'servers':{'demo':{'command':sys.executable,'timeout_seconds':{'call':121}}}},
    {'servers':{'demo':{'command':sys.executable,'tools':{'query':{'roles':['lead','lead']}}}}},
    {'servers':{'demo':{'command':sys.executable,'unknown':'field'}}},
])
def test_config_is_explicit_strict_and_host_owned(tmp_path,change):
    path=tmp_path/'bad.json';path.write_text(json.dumps({'schema_version':1,'servers':{},**change}))
    with pytest.raises(ConfigurationError):load_mcp_config(path)
    with pytest.raises(ConfigurationError):load_mcp_config('relative.json')


def test_env_is_minimal_and_duplicate_keys_rejected(setup_mcp,monkeypatch,tmp_path):
    monkeypatch.setenv('MCP_TEST_SECRET','fixture-value');monkeypatch.setenv('OTHER_SECRET','not-inherited')
    path,_=setup_mcp(env_from={'SERVER_TOKEN':'MCP_TEST_SECRET'})
    env=load_mcp_config(path).servers['demo'].environment()
    assert env['SERVER_TOKEN']=='fixture-value' and 'OTHER_SECRET' not in env and 'DEEPSEEK_API_KEY' not in env
    assert 'fixture-value' not in Path(path).read_text()
    Path(path).write_text('{"schema_version":1,"schema_version":1,"servers":{}}')
    with pytest.raises(ConfigurationError):load_mcp_config(path)


@pytest.mark.parametrize('schema',[
    {'type':'object','$ref':'https://example.invalid/schema'},
    {'type':'object','properties':{'x':{'$ref':'file:///etc/passwd'}}},
    {'type':'object','$ref':'#/$defs/missing'},
    {'type':'object','$schema':'https://example.invalid/dialect'},
    {'type':'object','$dynamicRef':'#'}, {'type':'array'},
    {'type':'object','description':'x'*33000}, {'type':'object','required':True},
])
def test_unrepresentable_schema_is_not_weakened(schema):
    with pytest.raises(Exception):schema_validator(schema)


def test_schema_local_refs_property_names_and_output_validation(setup_mcp):
    schema={'type':'object','$defs':{'n':{'type':'integer','minimum':2}},'properties':{'n':{'$ref':'#/$defs/n'},'$id':{'type':'string'}},'required':['n'],'default':{'$ref':'annotation only'}}
    v=schema_validator(schema);validate(v,{'n':2,'extra':'allowed','$id':'property'})
    with pytest.raises(MCPError):validate(v,{'n':1})
    from mini_claude_code.mcp.config import Grant
    b=Binding('demo','query','mcp__demo__query',1,'hash',Grant(),v,v)
    invalid=normalize({'content':[],'structuredContent':{'n':1}},b,{'sent':True,'request_id':12})
    assert invalid.status==ToolStatus.ERROR and invalid.metadata['sent'] and not invalid.metadata['execution_unknown']
    mixed=normalize({'content':[{'type':'text','text':'first'},{'type':'image','data':'MUST_NOT_STORE'}], 'structuredContent':{'n':2}},b,{'sent':True})
    assert mixed.status==ToolStatus.SUCCESS and mixed.metadata['incomplete'] and 'MUST_NOT_STORE' not in mixed.output
    assert normalize({'content':[{'type':'audio','data':'x'}]},replace(b,output_validator=None),{'sent':True}).status==ToolStatus.ERROR
    assert normalize({'isError':True,'content':[{'type':'text','text':'failed'}]},b,{'sent':True}).metadata['remote_is_error']


def test_snapshot_discovery_parameters_and_reconnect(make_runtime,setup_mcp):
    path,conn=setup_mcp()
    client=ScriptedModelClient(calls(call('connect','connect_mcp',name='demo'),call('early','mcp__demo__query',n=2)),
        calls(call('bad','mcp__demo__query',n=1),call('ok','mcp__demo__query',n=2,extra=True)),answer())
    r=make_runtime(client,mcp_config=path,mcp_connection_factory=conn);r.run('discover then query')
    assert [x.status for x in results(r)]==[ToolStatus.SUCCESS,ToolStatus.ERROR,ToolStatus.ERROR,ToolStatus.SUCCESS]
    assert len(conn.instances[0].calls)==1 and client.requests[1].tool_bindings['mcp__demo__query']['raw_tool_name']=='query'
    b=r.registry.get('mcp__demo__query').binding
    r.mcp.disconnect('demo');r.mcp.connect('demo')
    with pytest.raises(MCPError,match='stale_binding'):r.mcp.check_binding(b)
    assert conn.instances[0].closed and conn.instances[1].cwd==r.context.cwd


def test_atomic_pages_collisions_bad_directory_and_notifications(make_runtime,setup_mcp):
    path,conn=setup_mcp();conn.catalog=[tool('a.b'),tool('a?b'),tool('repeat'),tool('repeat'),tool('x'*80),tool()]
    r=make_runtime(ScriptedModelClient(),mcp_config=path,mcp_connection_factory=conn)
    info=r.mcp.connect('demo');assert info['tool_count']==1 and info['excluded'] and len(info['excluded'])==5
    old=r.registry.get('mcp__demo__query').binding
    conn.catalog=[tool(inputSchema={'type':'object','properties':{'n':{'type':'string'}}})]
    conn.instances[0].dirty.set()
    with pytest.raises(MCPError):r.mcp.check_binding(old)
    r.mcp.sync();assert r.registry.get('mcp__demo__query').binding.schema_fingerprint!=old.schema_fingerprint
    conn.instances[0].pages={None:{'tools':[tool()], 'nextCursor':'loop'},'loop':{'tools':[tool()], 'nextCursor':'loop'}}
    with pytest.raises(MCPError,match='游标'):r.mcp.refresh('demo')
    assert r.registry.get('mcp__demo__query') is None and conn.instances[0].closed


@pytest.mark.parametrize('count',[51])
def test_directory_limits_withdraw_instead_of_publishing_partial(make_runtime,setup_mcp,count):
    path,conn=setup_mcp();conn.catalog=[tool(str(i)) for i in range(count)]
    r=make_runtime(ScriptedModelClient(),mcp_config=path,mcp_connection_factory=conn)
    with pytest.raises(MCPError):r.mcp.connect('demo')
    assert not any(s.name.startswith('mcp__') for s in r.registry.specs()) and conn.instances[0].closed


def test_ask_denied_noninteractive_auto_filter_and_redaction(make_runtime,setup_mcp):
    grants={'query':{'policy':'ask','effect':'unknown'}};path,conn=setup_mcp(tools=grants)
    approvals=[];r=make_runtime(ScriptedModelClient(),mcp_config=path,mcp_connection_factory=conn,interactive=False,approve=lambda *a:approvals.append(a))
    r.mcp.connect('demo');out=r.executor.execute(call('q','mcp__demo__query',n=2),r.context)
    assert out.status==ToolStatus.DENIED and not approvals and not conn.instances[0].calls
    r.automatic=True
    assert not r.executor.visible(r.registry.get('mcp__demo__query'))
    assert r.executor.execute(call('c','connect_mcp',name='demo'),r.context).status==ToolStatus.DENIED
    r.automatic=False;r.context=replace(r.context,interactive=True)
    safe=[];r.mcp_approve=lambda binding,call,args,ctx:(safe.append(args) or True)
    out=r.executor.execute(call('redact','mcp__demo__query',n=2,api_key='hide',value=r.config.api_key),r.context)
    assert out.status==ToolStatus.SUCCESS and safe[0]['api_key']=='<redacted>' and safe[0]['value']=='<redacted>'


def test_approval_cannot_survive_schema_change(make_runtime,setup_mcp):
    path,conn=setup_mcp(tools={'query':{'policy':'ask','effect':'write'}})
    r=make_runtime(ScriptedModelClient(),mcp_config=path,mcp_connection_factory=conn)
    r.mcp.connect('demo')
    def approve(*args):r.mcp.disconnect('demo');r.mcp.connect('demo');return True
    r.mcp_approve=approve
    out=r.executor.execute(call('q','mcp__demo__query',n=2),r.context)
    assert out.status==ToolStatus.ERROR and 'stale_binding' in out.output and not any(c.calls for c in conn.instances)


def test_sent_unknown_side_effect_preserves_claim_and_never_replays(config,setup_mcp):
    path,conn=setup_mcp(tools={'query':{'policy':'allow','effect':'write'}});conn.fail=MCPError('transport lost')
    r=Runtime(config,interactive=False,client=ScriptedModelClient(),stream=False,mcp_config=path,mcp_connection_factory=conn)
    from test_teams_worktrees import task,invoke
    t=task(r);invoke(r,'claim_task',task_id=t.task_id)
    r.mcp.connect('demo');out=r.executor.execute(call('q','mcp__demo__query',n=2),r.context)
    assert out.metadata['execution_unknown'] and out.metadata['sent'] and len(conn.instances[0].calls)==1
    assert r.tasks.store.snapshot()[t.task_id].status=='in_progress' and r.mcp.has_work(t.task_id)
    with pytest.raises(StorageError):r.executor.execute(call('again','mcp__demo__query',n=2),r.context)
    assert len(conn.instances[0].calls)==1
    with pytest.raises(StorageError):r.close()
    # Teardown is safe and idempotent; claim remains durable for explicit inspection.
    assert r.tasks.store.snapshot()[t.task_id].status=='in_progress'


def test_child_role_intersection_and_independent_connections(make_runtime,setup_mcp):
    grants={'query':{'policy':'allow','effect':'read','roles':['lead','subagent']},'write':{'policy':'allow','effect':'write','roles':['lead','subagent']},'private':{'policy':'allow','effect':'read','roles':['subagent']}}
    path,conn=setup_mcp(tools=grants);conn.catalog=[tool(),tool('write'),tool('private')]
    r=make_runtime(ScriptedModelClient(),mcp_config=path,mcp_connection_factory=conn,child_client_factory=lambda cfg:ScriptedModelClient(answer()))
    r.mcp.connect('demo')
    child=r.task_runner.factory.create('child','analyze')
    try:
        child.run('analyze')
        assert child.registry.get('mcp__demo__query') and not child.registry.get('mcp__demo__write') and not child.registry.get('mcp__demo__private')
        assert child.registry.get('connect_mcp') is None and len(conn.instances)==2
        child.mcp.disconnect_all();assert not conn.instances[0].closed and conn.instances[1].closed
    finally:child.close()


def test_capacity_is_shared_and_bounded():
    c=Capacity()
    for _ in range(8):c.acquire()
    with pytest.raises(MCPError):c.acquire()
    for _ in range(8):c.release()
    assert c.count==0


def test_real_stdio_roundtrip_artifact_resume_and_cleanup(make_runtime,setup_mcp,config):
    server=Path(__file__).resolve().parents[1]/'examples/mcp_server.py'
    grants={'query':{'policy':'allow','effect':'read'},'write_note':{'policy':'ask','effect':'write'},'controlled_failure':{'policy':'allow','effect':'read'}}
    path,_=setup_mcp(args=[str(server)],tools=grants)
    client=ScriptedModelClient(calls(call('c','connect_mcp',name='demo')),
        calls(call('q','mcp__demo__query',text='hello'),call('w','mcp__demo__write_note',path='note.txt',content='fixture'),call('f','mcp__demo__controlled_failure')),answer())
    approvals=[];r=make_runtime(client,mcp_config=path,approve=lambda *a:(approvals.append(a) or True))
    r.run('controlled real local stdio fixture')
    assert [x.status for x in results(r)]==[ToolStatus.SUCCESS,ToolStatus.SUCCESS,ToolStatus.SUCCESS,ToolStatus.ERROR]
    assert (config.workspace/'note.txt').read_text()=='fixture' and len(approvals)==1
    assert results(r)[-1].metadata['remote_is_error'] and not results(r)[-1].metadata['execution_unknown']
    connection=r.mcp.entries['demo'].connection;hub=r.mcp.hub;sid=r.state.session_id
    assert connection.version=='2025-11-25' and connection.stderr
    r.close();assert connection.confirmed and connection.process.returncode is not None and not hub.thread.is_alive()
    restored=make_runtime(ScriptedModelClient(),resume=sid,mcp_config=path)
    assert restored.mcp.hub.thread is None and not restored.mcp.entries and not restored.registry.get('mcp__demo__query')


def test_real_stdio_timeout_is_unknown_without_replay(make_runtime,setup_mcp):
    server=Path(__file__).resolve().parents[1]/'examples/mcp_server.py'
    path,_=setup_mcp(args=[str(server)],timeout_seconds={'call':.15},tools={'controlled_failure':{'policy':'allow','effect':'read'}})
    r=make_runtime(ScriptedModelClient(calls(call('c','connect_mcp',name='demo')),calls(call('slow','mcp__demo__controlled_failure',delay=2)),answer()),mcp_config=path)
    r.run('timeout fixture')
    result=results(r)[-1]
    assert result.status==ToolStatus.ERROR and result.metadata['sent'] and result.metadata['execution_unknown']
    e=r.mcp.entries['demo'];assert e.state=='failed' and e.connection.confirmed and e.connection.process.returncode is not None
    assert not r.registry.get('mcp__demo__controlled_failure')


def test_dirty_during_discovery_is_not_lost(make_runtime,setup_mcp):
    path,conn=setup_mcp();original=conn.operation
    def operation(self,kind,*args,**kw):
        value=original(self,kind,*args,**kw)
        if kind=='list':self.dirty.set()
        return value
    conn.operation=operation
    r=make_runtime(ScriptedModelClient(),mcp_config=path,mcp_connection_factory=conn);r.mcp.connect('demo')
    b=r.registry.get('mcp__demo__query').binding
    assert conn.instances[0].dirty.is_set()
    with pytest.raises(MCPError):r.mcp.check_binding(b)
    conn.operation=original;r.mcp.sync();r.mcp.check_binding(r.registry.get('mcp__demo__query').binding)


def test_mcp_output_uses_existing_artifacts_hooks_and_unique_result(make_runtime,setup_mcp):
    path,conn=setup_mcp();conn.response={'content':[{'type':'text','text':'row\n'*18000}]}
    from mini_claude_code.hooks.manager import HookManager
    seen=[];hooks=HookManager();hooks.register('PostToolUse',lambda payload:seen.append(payload['result']['call_id']))
    def page(req):
        artifact=next(b.metadata['artifact_id'] for m in req.messages for b in m.content if isinstance(b,ToolResult) and b.call_id=='large')
        return calls(call('page','read_tool_output',artifact_id=artifact,offset=17999,limit=1))
    r=make_runtime(ScriptedModelClient(calls(call('c','connect_mcp',name='demo')),calls(call('large','mcp__demo__query',n=2)),page,answer()),mcp_config=path,mcp_connection_factory=conn,hooks=hooks)
    r.run('large result')
    rr=results(r);large=next(x for x in rr if x.call_id=='large')
    assert large.metadata['artifact_id'] and len(large.output)<=50000 and 'row' in rr[-1].output
    assert seen.count('large')==1 and len(conn.instances[0].calls)==1
    from mini_claude_code.core.loop import validate_history
    validate_history(r.state.messages)


def test_teammate_worktree_independent_binding_and_plan_gate(make_runtime,setup_mcp,config):
    from test_teams_worktrees import init_git,task,invoke,spawn,eventually
    init_git(config.workspace)
    path,conn=setup_mcp(tools={'query':{'policy':'allow','roles':['lead','teammate'],'effect':'read'},'write':{'policy':'allow','roles':['lead','teammate'],'effect':'write'}})
    conn.catalog=[tool(),tool('write')]
    child=ScriptedModelClient(calls(call('q','mcp__demo__query',n=2),call('w','mcp__demo__write',n=2)),answer())
    lead=make_runtime(ScriptedModelClient(),teams=True,child_client_factory=lambda cfg:child,mcp_config=path,mcp_connection_factory=conn)
    lead.mcp.connect('demo');t=task(lead)
    assert invoke(lead,'create_worktree',name='mcp-child',task_id=t.task_id).status==ToolStatus.SUCCESS
    worker=spawn(lead,task_id=t.task_id,require_plan=True)
    eventually(lambda:len(results(worker.runtime))>=2)
    rr=results(worker.runtime);assert [x.status for x in rr]==[ToolStatus.SUCCESS,ToolStatus.DENIED]
    assert len(conn.instances)==2 and conn.instances[1].cwd.endswith('/checkouts/mcp-child')
    assert conn.instances[0].cwd==str(config.workspace) and not conn.instances[0].calls
    assert len(conn.instances[1].calls)==1
    lead.close();assert all(c.closed for c in conn.instances) and worker.stopped.is_set()


def test_real_stdio_oversize_protocol_fails_without_poisoning_builtins(make_runtime,setup_mcp,config):
    script=config.workspace/'oversize.py'
    script.write_text('import sys\nsys.stdout.buffer.write(b"x"*(16*1024*1024+2)+b"\\n");sys.stdout.buffer.flush()\n')
    path,_=setup_mcp(args=[str(script)],timeout_seconds={'connect':2})
    r=make_runtime(ScriptedModelClient(),mcp_config=path)
    with pytest.raises(MCPError):r.mcp.connect('demo')
    e=r.mcp.entries['demo'];assert e.state=='failed' and e.connection.confirmed and e.connection.process.returncode is not None
    assert r.registry.get('read_file') and not r.registry.get('mcp__demo__query')
    r.close();assert not r.mcp.hub.thread.is_alive()


@pytest.mark.parametrize('policy,approved', [('allow', True), ('ask', True), ('ask', False)])
@pytest.mark.parametrize('secret_field', ['password', 'passwd', 'private_key'])
def test_call_display_redacted_before_approval_without_changing_execution(
        make_runtime, setup_mcp, monkeypatch, capsys, policy, approved, secret_field):
    from mini_claude_code.console import ConsoleBroker
    monkeypatch.setenv('DISPLAY_TEST_SECRET', 'synthetic-env-private')
    path, conn = setup_mcp(env_from={'SERVER_TOKEN': 'DISPLAY_TEST_SECRET'},
                          tools={'query': {'policy': policy, 'effect': 'read'}})
    args = {'n': 2, 'api_key': 'synthetic-field-private',
            'items': [{secret_field: 'synthetic-nested-private'}],
            'value': 'prefix offline-test-key synthetic-env-private suffix'}
    original = call('query', 'mcp__demo__query', **args)
    client = ScriptedModelClient(calls(original), answer())
    observed = []
    console = ConsoleBroker()
    def display(kind, value):
        if kind == 'call':
            observed.append(value)
        console.display(kind, value)
    r = make_runtime(client, mcp_config=path, mcp_connection_factory=conn, display=display)
    r.mcp.connect('demo')
    def approve(binding, call, safe, context):
        assert observed, 'Summary must already be safe before approval'
        assert safe == observed[-1].arguments
        return approved
    r.mcp_approve = approve
    r.run('display safety fixture')
    output = capsys.readouterr().out
    for secret in ('synthetic-field-private', 'synthetic-nested-private',
                   'offline-test-key', 'synthetic-env-private'):
        assert secret not in output
        assert secret not in repr(observed)
    assert '<redacted>' in output
    assert 'prefix' in output and 'suffix' in output
    assert original.arguments == args
    saved = [b for m in r.state.messages for b in m.content if isinstance(b, ToolCall)]
    assert saved[-1].arguments == args
    assert conn.instances[0].calls == ([('query', args)] if approved else [])
    assert results(r)[-1].status == (ToolStatus.SUCCESS if approved else ToolStatus.DENIED)
