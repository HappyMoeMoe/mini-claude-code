"""Task graphs, background execution, persistent events and scheduling."""
from datetime import datetime,timedelta,timezone
from dataclasses import replace
import threading
import pytest
from conftest import ScriptedModelClient,answer
from test_teams_worktrees import call,response,source,invoke,task,eventually
from mini_claude_code.core.errors import ModelError
from mini_claude_code.core.loop import validate_history
from mini_claude_code.core.messages import ToolResult,ToolStatus
from mini_claude_code.storage.task_store import TaskStore,view
from mini_claude_code.tasks.models import TaskError,Assignment
from mini_claude_code.scheduler.cron import CronExpression


def test_graph_atomic_claim_cycle_restart(config):
    store=TaskStore(config.workspace);a,b=source(),source()
    tasks=[store.create('a','',a),store.create('b','',a)]
    ready=threading.Barrier(3);outcomes=[]
    def link(one,two):
        ready.wait(5)
        try:store.add_dependencies(one.task_id,[two.task_id],a);outcomes.append(True)
        except TaskError:outcomes.append(False)
    threads=[threading.Thread(target=link,args=pair) for pair in (tasks,tasks[::-1])]
    for t in threads:t.start()
    ready.wait(5)
    for t in threads:t.join(3)
    assert sorted(outcomes)==[False,True]
    graph=TaskStore(config.workspace).snapshot()
    pending=next(r for r in graph.values() if view(r,graph)['ready'])
    ready=threading.Barrier(3);claimed=[]
    def claim(src):
        ready.wait(5)
        try:claimed.append(store.claim(pending.task_id,src))
        except TaskError:pass
    threads=[threading.Thread(target=claim,args=(s,)) for s in (a,b)]
    for t in threads:t.start()
    ready.wait(5)
    for t in threads:t.join(3)
    assert len(claimed)==1
    r=claimed[0];assignment=Assignment.from_record(r,str(config.workspace))
    wrong=b if r.owner['agent_id']==a['agent_id'] else a
    with pytest.raises(TaskError):store.complete(r.task_id,assignment,'',wrong)
    store.release(r.task_id,r.owner,r.revision,a,'controlled retry')
    newer=store.claim(r.task_id,a)
    with pytest.raises(TaskError):store.complete(r.task_id,assignment,'',a)
    with pytest.raises(TaskError):store.claim(next(t.task_id for t in tasks if t.task_id!=r.task_id),a)
    updated,new_ready,changed=store.complete(r.task_id,Assignment.from_record(newer,str(config.workspace)),'verified',a)
    assert changed and len(new_ready)==1 and TaskStore(config.workspace).snapshot()[r.task_id].status=='completed'


def test_background_one_result_noninteractive_event(config,make_runtime,monkeypatch):
    approvals=[]
    client=ScriptedModelClient(response(call('bash',command='printf M3_BACKGROUND',run_in_background=True)),answer())
    r=make_runtime(client,approve=lambda c,a,ctx:(approvals.append(a) or True))
    r.run('launch explicitly')
    entries=list(r.events.background.work.values())
    for entry in entries:entry.thread.join(3)
    r.events.boundary();record=r.events.background.records()[0]
    assert record['status']=='completed' and record['artifact_id']
    assert 'M3_BACKGROUND' in r.artifacts.resolve(record['artifact_id'])[1].read_text()
    assert len([b for m in r.state.messages for b in m.content if isinstance(b,ToolResult)])==1
    client.steps=[response(call('bash',command='touch forbidden'),call('claim_task',task_id='task_'+'0'*32)),answer()]
    monkeypatch.setattr('builtins.input',lambda *a:(_ for _ in ()).throw(AssertionError('auto stdin')))
    r.events.dispatch()
    assert len(approvals)==1 and not (config.workspace/'forbidden').exists()
    auto=[b for m in r.state.messages for b in m.content if isinstance(b,ToolResult)][1:]
    assert all(b.status==ToolStatus.DENIED for b in auto)
    assert r.events.store.list(r.state.session_id)[0]['status']=='accepted';validate_history(r.state.messages)


def test_background_blocks_completion_and_close_releases(config,make_runtime):
    r=make_runtime(ScriptedModelClient(response(call('bash',command='sleep 60',run_in_background=True)),answer()))
    t=task(r);r.tasks.claim_host(t.task_id);r.run('background task')
    assert invoke(r,'complete_task',task_id=t.task_id).status==ToolStatus.ERROR
    r.close()
    assert r.tasks.store.snapshot()[t.task_id].owner is None
    assert all(not w.thread.is_alive() for w in r.events.background.work.values())
    assert r.events.background.records()[0]['status']=='cancelled'


def test_cron_dedupe_held_retry_and_resume(config,make_runtime):
    fixed=datetime(2026,11,17,8,tzinfo=timezone.utc)
    client=ScriptedModelClient(response(call('schedule_cron',cron='* * * * *',prompt='report',timezone='Asia/Shanghai')),answer())
    r=make_runtime(client);r.events.cron.clock=lambda:fixed;r.run('schedule')
    r.events.cron.tick_once(fixed+timedelta(minutes=1));r.events.cron.tick_once(fixed+timedelta(minutes=1))
    assert len(r.events.pending())==1
    client.steps=[ModelError('controlled failure')]
    with pytest.raises(ModelError):r.events.dispatch()
    row=next(x for x in r.events.store.list(r.state.session_id) if x['status']=='held')
    count=len(r.state.messages);r.events.cron.tick_once(fixed+timedelta(minutes=2))
    assert not r.events.pending()
    assert next(iter(r.events.cron.store.snapshot()['jobs'].values()))['skipped']==1
    client.steps=[answer()];r.events.retry(row['event']['event_id']);r.events.dispatch()
    assert len(r.state.messages)==count+1
    assert r.events.store.get(row['event']['event_id'])['message_id']==row['message_id']
    r.events.cron.tick_once(fixed+timedelta(minutes=3));sid=r.state.session_id;r.close()
    fresh=ScriptedModelClient(answer());restored=make_runtime(fresh,resume=sid)
    assert not fresh.requests and not restored.events.pending()
    assert any(x['status']=='held' for x in restored.events.store.list(sid))


@pytest.mark.parametrize('text',['* * * *','60 * * * *','*/0 * * * *','1-4/2 * * * *','0 0 * JAN *','0 0 L * *'])
def test_bad_cron(text):
    with pytest.raises(ValueError):CronExpression(text)


def test_cron_dst_and_leader(config,make_runtime):
    expression=CronExpression('30 1 * * *')
    assert all(expression.matches(datetime(2026,11,1,h,30,tzinfo=timezone.utc),'America/New_York') for h in (5,6))
    assert CronExpression('0 0 31 * 1').matches(datetime(2026,11,2,tzinfo=timezone.utc),'UTC')
    a=make_runtime(ScriptedModelClient(answer()));b=make_runtime(ScriptedModelClient(answer()))
    assert a.events.cron._leader() and not b.events.cron._leader()
    a.events.cron.close();assert b.events.cron._leader()


def test_cron_acceptance_repair_does_not_replay_tools(config,make_runtime,monkeypatch):
    from mini_claude_code.storage.event_store import QueueError
    from mini_claude_code.core.errors import StorageError
    fixed=datetime(2026,11,17,tzinfo=timezone.utc)
    client=ScriptedModelClient(response(call('write_file',path='must_not_replay',content='x')))
    r=make_runtime(client);r.events.cron.clock=lambda:fixed
    r.events.cron.schedule(dict(cron='* * * * *',prompt='only report',timezone='UTC',durable=True,recurring=False),r.context)
    r.events.cron.tick_once(fixed+timedelta(minutes=1));original=r.events.cron.delivery
    def interrupted(event,action,*args):
        if action=='accept':raise QueueError('injected source ack failure')
        return original(event,action,*args)
    with monkeypatch.context() as context:
        context.setattr(r.events.cron,'delivery',interrupted)
        with pytest.raises(StorageError):r.events.dispatch()
    sid=r.state.session_id;r.close();fresh=ScriptedModelClient(answer())
    restored=make_runtime(fresh,resume=sid)
    assert not fresh.requests and not (config.workspace/'must_not_replay').exists()
    assert next(iter(restored.events.cron.store.snapshot()['jobs'].values()))['status']=='completed'
    validate_history(restored.state.messages)


def test_cancel_cron_in_flight_records_acceptance(config,make_runtime):
    fixed=datetime(2026,11,17,tzinfo=timezone.utc)
    client=ScriptedModelClient();r=make_runtime(client);r.events.cron.clock=lambda:fixed
    import json
    job=json.loads(r.events.cron.schedule(dict(cron='* * * * *',prompt='report',timezone='UTC',durable=True,recurring=False),r.context).output)
    r.events.cron.tick_once(fixed+timedelta(minutes=1));event=r.events.pending()[0]['event']['event_id']
    def cancel(req):r.events.cron.cancel({'job_id':job['job_id']},r.context);return answer()
    client.steps=[cancel];r.events.dispatch()
    row=r.events.store.get(event)
    assert row['status']=='accepted' and row['cancel_requested']


def test_background_crash_unknown_never_signals_saved_pid(config,make_runtime,monkeypatch):
    import os
    client=ScriptedModelClient(response(call('bash',command='printf crash',run_in_background=True)),answer())
    from mini_claude_code.runtime import Runtime
    r=Runtime(config,interactive=True,client=client,approve=lambda *args:True,stream=False)
    with monkeypatch.context() as patch:
        patch.setattr(r.events.background,'drain',lambda:None)
        r.run('launch')
    for w in r.events.background.work.values():w.thread.join(3)
    # Stop actual threads first, intentionally omit final record/event commit.
    r.events.cron.stop_signal.set();r.events.cron.thread.join(3)
    if r.events.cron.leader is not None:os.close(r.events.cron.leader);r.events.cron.leader=None
    sid=r.state.session_id;r.store.close();r.store=None;r.client.close();r.events.wakeup.close()
    r.tasks.release_lease()
    monkeypatch.setattr('os.killpg',lambda *a:(_ for _ in ()).throw(AssertionError('saved PID signal')))
    recovered=make_runtime(ScriptedModelClient(answer()),resume=sid)
    assert recovered.events.background.records()[0]['status']=='execution_unknown'
    assert recovered.events.background.usage()>0
    # The abandoned fixture must not execute a normal close against unfinished host work.
    r.events.background.work.clear()
