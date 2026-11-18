"""Teams and worktrees with temporary Git repositories and controlled synchronization."""
from pathlib import Path
import json
import subprocess
import threading
import time
import pytest
from conftest import ScriptedModelClient,answer
from mini_claude_code.core._validation import new_id
from mini_claude_code.core.loop import validate_history
from mini_claude_code.core.messages import ToolCall,ToolResult,ToolStatus
from mini_claude_code.core.model import ModelResponse,StopReason
from mini_claude_code.core.session import AgentRole
from mini_claude_code.storage.task_store import view
from mini_claude_code.tasks.models import TaskError
from mini_claude_code.cli import main,build_parser


def call(tool_name,**args):return ToolCall(new_id('message'),tool_name,args)
def response(*calls):return ModelResponse(list(calls),StopReason.TOOL_CALL)
def source():return {'agent_id':new_id('agent'),'session_id':new_id('session'),'assistant_message_id':None,'call_id':None,'kind':'host'}
def invoke(r,tool_name,**args):return r.executor.execute(call(tool_name,**args),r.context,assistant_message_id=new_id('message'))
def task(r,subject='work'):return r.tasks.store.create(subject,'bounded edit',r.tasks.source('host'),team_id=r.team_id)
def spawn(r,name='alice',**kw):
    result=invoke(r,'spawn_teammate',name=name,**kw)
    assert result.status==ToolStatus.SUCCESS,result.output
    return r.team.workers[json.loads(result.output)['agent_id']]
def eventually(predicate,timeout=6):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        value=predicate()
        if value:return value
        threading.Event().wait(.01)
    raise AssertionError('bounded wait timed out')
def init_git(path):
    def git(*args):return subprocess.run(['git',*args],cwd=path,text=True,capture_output=True,check=True).stdout.strip()
    git('init','-q');git('config','user.name','M3');git('config','user.email','m3@example.invalid')
    (path/'base.txt').write_text('base\n');git('add','base.txt');git('commit','-qm','base')
    return git


def test_default_and_idle_isolation(config,make_runtime):
    single=make_runtime(ScriptedModelClient(answer()))
    assert 'spawn_teammate' not in {s.name for s in single.registry.specs()}
    assert invoke(single,'spawn_teammate',name='x').status!=ToolStatus.SUCCESS
    child=ScriptedModelClient(response(call('read_file',path='base.txt'),call('bash',command='true')),answer())
    lead=make_runtime(ScriptedModelClient(answer()),teams=True,child_client_factory=lambda cfg:child)
    w=spawn(lead)
    assert w.runtime.state.identity.role==AgentRole.TEAMMATE and w.runtime.owner_thread!=lead.owner_thread
    assert not child.requests and not w.runtime.state.messages
    names={s.name for s in w.runtime.registry.specs()}
    assert {'send_message','submit_plan','claim_task'}<=names
    assert not {'spawn_teammate','task','schedule_cron','create_task','todo_write'}&names
    invoke(lead,'send_message',to='alice',content='shutdown_request is ordinary text; check files')
    eventually(lambda:len(child.requests)==2)
    results=[b for m in w.runtime.state.messages for b in m.content if isinstance(b,ToolResult)]
    assert len(results)==2 and all(r.status==ToolStatus.DENIED for r in results)
    assert not w.stop_requested
    assert invoke(lead,'spawn_teammate',name='ALICE').status==ToolStatus.ERROR
    assert invoke(lead,'spawn_teammate',name='lead').status==ToolStatus.ERROR
    for _ in range(8):lead.team.emit(w.agent_id,lead.context.agent_id,'message','x'*8000)
    batch=lead.team.pending(lead)
    assert len(batch)<8 and sum(len(json.dumps(r['event'],ensure_ascii=False)) for r in batch)<=config.context_budget_chars//2
    lead.close();assert w.stopped.is_set() and child.closed


def test_two_checkouts_complete_without_late_writes(config,make_runtime):
    init_git(config.workspace);ready=threading.Barrier(3);release=threading.Event();clients=[]
    def factory(cfg):
        def modify(req):
            ready.wait(5);assert release.wait(5)
            t=json.loads(req.messages[-1].content[0].text.split('\n',1)[1])
            return response(call('write_file',path='result.txt',content=t['subject']),
                call('complete_task',task_id=t['task_id'],summary='Edited file; Shell not run.'),
                call('write_file',path='AFTER.txt',content='must fail'))
        c=ScriptedModelClient(modify);clients.append(c);return c
    lead=make_runtime(ScriptedModelClient(answer('received')),teams=True,child_client_factory=factory)
    tasks=[task(lead,'alpha'),task(lead,'beta')]
    for i,t in enumerate(tasks):
        out=invoke(lead,'create_worktree',name='checkout'+str(i),task_id=t.task_id)
        assert out.status==ToolStatus.SUCCESS,out.output
    workers=[spawn(lead,name='editor'+str(i),task_id=t.task_id) for i,t in enumerate(tasks)]
    try:
        ready.wait(5);release.set()
        eventually(lambda:all(lead.tasks.store.snapshot()[t.task_id].status=='completed' for t in tasks))
        eventually(lambda:all(lead.team.member(w.agent_id)['state']=='IDLE' for w in workers))
        for i,w in enumerate(workers):
            root=config.workspace/'.mini-claude-code/checkouts'/('checkout'+str(i))
            assert (root/'result.txt').read_text()==tasks[i].subject and not (root/'AFTER.txt').exists()
            validate_history(w.runtime.state.messages)
            results=[b for m in w.runtime.state.messages for b in m.content if isinstance(b,ToolResult)]
            assert [r.status for r in results]==[ToolStatus.SUCCESS,ToolStatus.SUCCESS,ToolStatus.DENIED]
        assert not (config.workspace/'result.txt').exists()
        assert lead.events.pending();lead.events.dispatch()
        assert any(m.origin and m.origin['kind']=='team' for m in lead.state.messages)
        assert all(not m.is_human for m in lead.state.messages)
        with pytest.raises(TaskError):lead.tasks.worktrees.remove('checkout0')
    finally:release.set();lead.close()


def test_plan_revisions_gate_and_noninteractive_shell(config,make_runtime):
    ids=[]
    def first(req):
        t=json.loads(req.messages[-1].content[0].text.split('\n',1)[1]);ids.append(t['task_id'])
        return response(call('write_file',path='early.txt',content='bad'),call('submit_plan',plan='Read then edit.'))
    def final(req):return response(call('write_file',path='allowed.txt',content='ok'),call('bash',command='true'),call('complete_task',task_id=ids[0]))
    child=ScriptedModelClient(first,response(call('write_file',path='rejected.txt',content='bad'),call('submit_plan',plan='Revised plan.')),final)
    lead=make_runtime(ScriptedModelClient(answer()),teams=True,child_client_factory=lambda cfg:child)
    t=task(lead);w=spawn(lead,task_id=t.task_id,require_plan=True)
    def pending():
        m=lead.team.member(w.agent_id)
        return m['latest_plan'] if m['plan_status']=='pending' and m['state']=='WAITING_APPROVAL' else None
    first_id=eventually(pending)
    assert not (config.workspace/'early.txt').exists() and len(child.requests)==1
    assert invoke(lead,'review_plan',request_id=first_id,approve=False,feedback='Reduce scope').status==ToolStatus.SUCCESS
    second=eventually(lambda:(p if (p:=pending()) and p!=first_id else None))
    assert not (config.workspace/'rejected.txt').exists()
    assert invoke(lead,'review_plan',request_id=first_id,approve=True).status==ToolStatus.ERROR
    assert invoke(lead,'review_plan',request_id=second,approve=True).status==ToolStatus.SUCCESS
    eventually(lambda:lead.tasks.store.snapshot()[t.task_id].status=='completed')
    assert (config.workspace/'allowed.txt').read_text()=='ok'
    results=[b for m in w.runtime.state.messages for b in m.content if isinstance(b,ToolResult)]
    assert next(r for r in results if r.metadata['tool_name']=='bash').status==ToolStatus.DENIED
    assert all(r.get('acknowledgement_kind')=='host_applied' for r in lead.events.store.list(w.runtime.state.session_id) if r['event']['kind']=='team_control')


def test_scope_adoption_and_directory_lease(config,make_runtime):
    lead=make_runtime(ScriptedModelClient(answer()),teams=True,child_client_factory=lambda cfg:ScriptedModelClient(answer()))
    other=lead.tasks.store.create('foreign','',source())
    assert invoke(lead,'spawn_teammate',name='foreign',task_id=other.task_id).status==ToolStatus.ERROR
    owned=lead.tasks.store.create('owned','',lead.tasks.source('host'))
    w=spawn(lead,name='adopted',task_id=owned.task_id)
    eventually(lambda:lead.team.member(w.agent_id)['state']=='WORKING')
    assert lead.tasks.store.snapshot()[owned.task_id].team_id==lead.team_id
    assert invoke(lead,'write_file',path='conflict',content='bad').status==ToolStatus.ERROR
    t=task(lead,'another')
    assert invoke(lead,'spawn_teammate',name='collision',task_id=t.task_id).status==ToolStatus.ERROR
    assert lead.tasks.store.snapshot()[t.task_id].owner is None
    lead.close();assert lead.tasks.store.snapshot()[owned.task_id].status=='pending'


def test_partial_git_creation_and_broken_binding(config,make_runtime,monkeypatch):
    init_git(config.workspace);lead=make_runtime(ScriptedModelClient(answer()));t=task(lead)
    service=lead.tasks.worktrees;original=service.git
    def fail_after_add(*args,**kw):
        if args[:2]==('worktree','add'):
            original(*args,**kw)
            graph=lead.tasks.store.snapshot();assert not view(graph[t.task_id],graph)['ready']
            with pytest.raises(TaskError):lead.tasks.store.claim(t.task_id,lead.tasks.source('host'))
            raise TaskError('injected after successful add')
        return original(*args,**kw)
    monkeypatch.setattr(service,'git',fail_after_add)
    with pytest.raises(TaskError,match='partial'):service.create('partial',t.task_id,'HEAD',lead.tasks.source('host'))
    assert (config.workspace/'.mini-claude-code/checkouts/partial').is_dir()
    assert service.records()[0]['status']=='partial'
    with pytest.raises(TaskError):lead.tasks.claim_host(t.task_id)
    monkeypatch.setattr(service,'git',original);t2=task(lead)
    service.create('good',t2.task_id,'HEAD',lead.tasks.source('host'));lead.tasks.claim_host(t2.task_id)
    original('symbolic-ref','HEAD','refs/heads/unexpected',cwd=lead.context.cwd)
    assert invoke(lead,'write_file',path='bad.txt',content='x').status!=ToolStatus.SUCCESS
    assert not (config.workspace/'bad.txt').exists()


def test_shutdown_ack_after_cleanup(config,make_runtime,monkeypatch):
    entered=threading.Event();finish=threading.Event()
    import mini_claude_code.tools.filesystem as fs
    original=fs._atomic_write
    def blocked(path,content):entered.set();assert finish.wait(5);return original(path,content)
    monkeypatch.setattr(fs,'_atomic_write',blocked)
    child=ScriptedModelClient(response(call('write_file',path='out.txt',content='ok')),answer())
    lead=make_runtime(ScriptedModelClient(answer()),teams=True,child_client_factory=lambda cfg:child)
    t=task(lead);w=spawn(lead,task_id=t.task_id)
    try:
        assert entered.wait(5)
        out=invoke(lead,'request_shutdown',teammate='alice');assert out.status==ToolStatus.SUCCESS
        row=json.loads(out.output.split('\n',1)[0]);assert row['status']=='pending'
        assert lead.tasks.store.snapshot()[t.task_id].owner is not None and not w.stopped.is_set()
    finally:finish.set()
    assert w.stopped.wait(5)
    assert lead.tasks.store.snapshot()[t.task_id].owner is None
    assert lead.team.store.request(row['request_id'])['status']=='stopped'
    assert (config.workspace/'out.txt').read_text()=='ok';validate_history(w.runtime.state.messages)


def test_management_needs_no_model(config,monkeypatch):
    monkeypatch.setenv('DEEPSEEK_API_KEY','')
    assert build_parser().parse_args(['chat','--teams']).teams
    assert main(['--workspace',str(config.workspace),'worktrees','list'])==0
    assert not (config.workspace/'.mini-claude-code/worktrees').exists()


def test_auto_claim_failure_block_and_fresh_plan(config,make_runtime):
    init_git(config.workspace)
    child=ScriptedModelClient()
    lead=make_runtime(ScriptedModelClient(answer()),teams=True,child_client_factory=lambda cfg:child)
    tasks=[task(lead,'one'),task(lead,'two')]
    for i,t in enumerate(tasks):lead.tasks.worktrees.create('auto'+str(i),t.task_id,'HEAD',lead.tasks.source('host'),team_id=lead.team_id)
    claims=[]
    def request_plan(req):
        t=json.loads(req.messages[-1].content[0].text.split('\n',1)[1]);claims.append(t['task_id'])
        return response(call('submit_plan',plan='bounded task plan'))
    def complete(req):return response(call('complete_task',task_id=claims[-1],summary='no edits needed'))
    child.steps=[request_plan,complete,request_plan]
    w=spawn(lead,auto_claim=True,require_plan=True)
    first=eventually(lambda:lead.team.member(w.agent_id)['latest_plan'])
    assert invoke(lead,'review_plan',request_id=first,approve=True).status==ToolStatus.SUCCESS
    second=eventually(lambda:(p if (p:=lead.team.member(w.agent_id)['latest_plan']) and p!=first else None))
    assert len(claims)==2 and claims[0]!=claims[1]
    assert lead.team.member(w.agent_id)['plan_status']=='pending'
    assert invoke(lead,'review_plan',request_id=first,approve=True).status==ToolStatus.ERROR
    lead.close()
    assert lead.tasks.store.snapshot()[claims[1]].status=='pending'


def test_teammate_scope_get_and_worktree_skills(config,make_runtime):
    init_git(config.workspace);observed=threading.Event();seen={}
    lead=make_runtime(ScriptedModelClient(answer()),teams=True,child_client_factory=lambda cfg:child)
    foreign=lead.tasks.store.create('other','',source())
    t=task(lead);lead.tasks.worktrees.create('scoped',t.task_id,'HEAD',lead.tasks.source('host'),team_id=lead.team_id)
    root=config.workspace/'.mini-claude-code/checkouts/scoped'
    (root/'skills/a').mkdir(parents=True);(root/'skills/a/SKILL.md').write_text('---\nname: local\ndescription: local checkout skill\n---\nOnly local root.')
    (config.workspace/'skills/b').mkdir(parents=True);(config.workspace/'skills/b/SKILL.md').write_text('---\nname: main\ndescription: main only\n---\nmain')
    def checks(req):
        seen['system']=req.system
        return response(call('get_task',task_id=foreign.task_id),call('load_skill',name='local'),
            call('read_file',path=str(config.workspace/'base.txt')),call('read_file',path='.git'))
    def stop(req):observed.set();return answer()
    child=ScriptedModelClient(checks,stop)
    w=spawn(lead,task_id=t.task_id);assert observed.wait(5)
    results=[b for m in w.runtime.state.messages for b in m.content if isinstance(b,ToolResult)]
    assert [r.status for r in results]==[ToolStatus.ERROR,ToolStatus.SUCCESS,ToolStatus.DENIED,ToolStatus.DENIED]
    assert 'local checkout skill' in seen['system'] and 'main only' not in seen['system']


def test_model_concurrency_two_and_cancel_waiter():
    from mini_claude_code.teams.manager import LimitedClient
    from mini_claude_code.core.cancellation import CancellationToken,Cancelled
    gate=threading.BoundedSemaphore(2);inside=threading.Barrier(3);release=threading.Event()
    active=[];errors=[];token=CancellationToken()
    class Client:
        def generate(self,req):inside.wait(5);assert release.wait(5);return answer()
    clients=[LimitedClient(Client(),gate,lambda:None) for _ in range(2)]
    def run(c):
        try:c.generate(None)
        except BaseException as exc:errors.append(exc)
    threads=[threading.Thread(target=run,args=(c,)) for c in clients]
    for t in threads:t.start()
    inside.wait(5)
    waiter=LimitedClient(Client(),gate,lambda:token)
    third=threading.Thread(target=run,args=(waiter,));third.start();token.cancel();third.join(2)
    assert not third.is_alive() and isinstance(errors[0],Cancelled)
    release.set()
    for t in threads:t.join(2)
    assert len(errors)==1


def test_clean_worktree_remove_preserves_branch(config,make_runtime):
    git=init_git(config.workspace);r=make_runtime(ScriptedModelClient(answer()));t=task(r)
    r.tasks.worktrees.create('clean',t.task_id,'HEAD',r.tasks.source('host'))
    r.tasks.claim_host(t.task_id);assert invoke(r,'complete_task',task_id=t.task_id).status==ToolStatus.SUCCESS
    r.tasks.batch_boundary()
    row=r.tasks.worktrees.remove('clean')
    assert row['status']=='removed' and not Path(row['path']).exists()
    assert git('branch','--list','mini/clean').strip()=='mini/clean'


def test_restore_team_never_restarts_threads_or_approval(config,make_runtime):
    from mini_claude_code.teams.store import TeamStore
    lead=make_runtime(ScriptedModelClient(answer()),teams=True,child_client_factory=lambda cfg:ScriptedModelClient(answer()))
    w=spawn(lead);sid=lead.state.session_id;team_id=lead.team_id;member_id=w.agent_id
    lead.close()
    store=TeamStore(config.workspace,team_id)
    # Controlled crash residue after all actual resources were stopped.
    with store.transaction() as data:data['members'][member_id].update(state='WORKING',plan_status='approved')
    client=ScriptedModelClient(answer())
    restored=make_runtime(client,resume=sid,teams=True)
    assert not client.requests and not restored.team.workers and restored.team_id!=team_id
    assert store.read()['members'][member_id]['state']=='interrupted'
    assert store.read()['members'][member_id]['plan_status']!='approved'


def test_failed_task_not_automatically_retried(config,make_runtime):
    from mini_claude_code.core.errors import ModelError
    init_git(config.workspace)
    clients=[]
    def factory(cfg):
        c=ScriptedModelClient(ModelError('controlled work failure'));clients.append(c);return c
    lead=make_runtime(ScriptedModelClient(answer()),teams=True,child_client_factory=factory)
    t=task(lead);lead.tasks.worktrees.create('failed',t.task_id,'HEAD',lead.tasks.source('host'),team_id=lead.team_id)
    w=spawn(lead,task_id=t.task_id);assert w.stopped.wait(5)
    r=lead.tasks.store.snapshot()[t.task_id]
    assert r.status=='pending' and r.auto_claim_blocked
    idle=spawn(lead,name='idle',auto_claim=True)
    # Force an auto scan via a controlled condition wake; blocked tasks remain untouched.
    idle.notify()
    assert not clients[1].requests
    assert lead.tasks.store.snapshot()[t.task_id].owner is None


def test_team_capacity_and_message_bound(config,make_runtime):
    lead=make_runtime(ScriptedModelClient(answer()),teams=True,child_client_factory=lambda cfg:ScriptedModelClient(answer()))
    workers=[spawn(lead,name='idle'+str(i)) for i in range(3)]
    assert invoke(lead,'spawn_teammate',name='fourth').status==ToolStatus.ERROR
    # Persisting a message must precede waking. Hold the receiver's wake to avoid consumption.
    target=workers[0];original=target.notify;target.notify=lambda:None
    # The independent message cap is checked against actual durable pending/held rows.
    from mini_claude_code.core.events import RuntimeEvent
    from mini_claude_code.storage.event_store import EventStore
    store=EventStore(config.workspace);put=store.put
    # Use held fixtures so sleeping receiver cannot drain them during the capacity check.
    for i in range(500):
        event=RuntimeEvent('team_message',target.runtime.state.session_id,
            {'team_id':lead.team_id,'sender':lead.context.agent_id,'target':target.agent_id,'type':'message','content':'bounded','request_id':None},
            source_agent_id=lead.context.agent_id,source_id=lead.team_id)
        # Large fixture writes bypass capacity scan, but retain actual codec and atomic write.
        from mini_claude_code.storage.event_store import atomic_json
        row={'schema_version':1,'event':event.to_dict(),'status':'held','message_id':None,'attempt_id':None,'response_id':None,'cancel_requested':False}
        atomic_json(config.workspace,store.path(event.event_id),store._validate(row))
    assert invoke(lead,'send_message',to='idle0',content='overflow').status==ToolStatus.ERROR
    assert invoke(lead,'request_shutdown',teammate='idle0').status==ToolStatus.SUCCESS
    target.notify=original;original();assert target.stopped.wait(5)


def test_incomplete_worktree_recovery_never_promotes(config,make_runtime):
    from dataclasses import replace
    from mini_claude_code.storage.event_store import atomic_json
    init_git(config.workspace);r=make_runtime(ScriptedModelClient(answer()));t=task(r);service=r.tasks.worktrees
    row=service.create('incomplete',t.task_id,'HEAD',r.tasks.source('host'))
    with r.tasks.store._locked():
        current=r.tasks.store._get(r.tasks.store._read_all(),t.task_id)
        r.tasks.store._write(replace(current,worktree={**current.worktree,'status':'preparing'},revision=current.revision+1))
    row['status']='preparing';atomic_json(config.workspace,service._path(row['operation_id']+'.json'),row)
    service.recover();current=r.tasks.store.snapshot()[t.task_id]
    assert current.worktree['status']=='partial'
    assert service.records()[0]['observed_registration']['branch']=='refs/heads/mini/incomplete'
    assert Path(row['path']).exists()
    with pytest.raises(TaskError):r.tasks.claim_host(t.task_id)


def test_lead_worktree_batch_switch_and_shell_cwd(config,make_runtime):
    git=init_git(config.workspace);observed=[]
    client=ScriptedModelClient()
    lead=make_runtime(client,approve=lambda c,a,ctx:(observed.append(ctx.cwd) or True))
    t=task(lead);row=lead.tasks.worktrees.create('lead',t.task_id,'HEAD',lead.tasks.source('host'))
    lead.tasks.claim_host(t.task_id)
    client.steps=[response(call('bash',command='pwd'),call('complete_task',task_id=t.task_id),call('write_file',path='late',content='bad')),
                  response(call('write_file',path='next_request',content='main')),answer()]
    lead.run('bounded work then independent main work')
    assert observed==[row['path']]
    assert not (Path(row['path'])/'late').exists() and not (config.workspace/'late').exists()
    assert (config.workspace/'next_request').read_text()=='main'
    validate_history(lead.state.messages)


def test_cancel_lead_keeps_persistent_teammate_and_auto_cannot_spawn(config,make_runtime,monkeypatch):
    from mini_claude_code.core.cancellation import CancellationToken
    from mini_claude_code.core.loop import TurnOutcome
    token=CancellationToken()
    def cancel(req):token.cancel();return answer()
    client=ScriptedModelClient(cancel)
    lead=make_runtime(client,teams=True,child_client_factory=lambda cfg:ScriptedModelClient(answer()))
    w=spawn(lead)
    assert lead.run('cancel only this turn',cancellation=token)==TurnOutcome.CANCELLED
    assert not w.stop_requested and w.thread.is_alive()
    client.steps=[response(call('spawn_teammate',name='forbidden'),call('create_task',subject='forbidden')),
                  answer()]
    monkeypatch.setattr('builtins.input',lambda *args:(_ for _ in ()).throw(AssertionError('auto input')))
    lead.events.dispatch()
    results=[b for m in lead.state.messages for b in m.content if isinstance(b,ToolResult)]
    assert all(r.status==ToolStatus.DENIED for r in results)
    assert len(lead.team.workers)==1
