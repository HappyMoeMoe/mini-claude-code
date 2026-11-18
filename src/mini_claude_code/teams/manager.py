"""Persistent teammate threads reuse Runtime, AgentLoop and ToolExecutor."""
from dataclasses import dataclass
from contextlib import contextmanager, ExitStack
import json
import queue
import threading
import time

from ..core._validation import new_id, utc_now
from ..core.cancellation import CancellationToken, Cancelled
from ..core.events import RuntimeEvent
from ..core.errors import StorageError
from ..core.loop import TurnOutcome
from ..core.messages import Message, MessageRole, TextBlock, ToolSpec
from ..core.session import AgentIdentity, AgentRole
from ..storage.event_store import EventStore, file_lock
from ..storage.session_store import SessionStore, durable_section
from ..storage.locking import SessionLock
from ..tasks.models import TaskError
from ..tools.registry import ToolOutput
from ..worktrees.service import safe_name
from .store import TeamStore

TEAMMATE_TOOLS=frozenset({'send_message','submit_plan','list_tasks','get_task','claim_task','complete_task',
    'read_file','write_file','edit_file','glob','bash','load_skill','read_tool_output','compact'})
COORDINATION=frozenset({'send_message','list_teammates','review_plan','request_shutdown'})
PROJECT=frozenset({'read_file','write_file','edit_file','glob','bash','load_skill','task'})
SIDE_EFFECTS=frozenset({'write_file','edit_file','bash','complete_task','task'})
STARTUP_TIMEOUT = 30.0
STARTUP_STOP_TIMEOUT = 3.0


class LimitedClient:
    """Limit actual requests, including auxiliary ones, through cancellable admission."""
    def __init__(self, client, semaphore, token):self.inner,self.semaphore,self.token=client,semaphore,token
    def _acquire(self):
        while not self.semaphore.acquire(timeout=.05):
            token=self.token()
            if token is not None:token.check()
        token=self.token()
        if token is not None and token.cancelled:self.semaphore.release();token.check()
    def generate(self,request):
        self._acquire()
        try:return self.inner.generate(request)
        finally:self.semaphore.release()
    def stream(self,request):
        self._acquire()
        try:yield from self.inner.stream(request)
        finally:self.semaphore.release()
    def cancel_active(self):return self.inner.cancel_active()
    def close(self):return self.inner.close()


@dataclass(frozen=True)
class TeammateSpec:
    manager: object
    agent_id: str
    session_id: str
    @property
    def identity(self):return AgentIdentity(self.agent_id,AgentRole.TEAMMATE,self.manager.lead.context.agent_id)
    def linkage(self):return {'team_id':self.manager.team_id,'lead_session_id':self.manager.lead.state.session_id}


class Worker:
    def __init__(self,manager,member,task_id):
        self.manager,self.agent_id,self.task_id=manager,member['agent_id'],task_id
        self.condition=threading.Condition();self.started=threading.Event();self.stopped=threading.Event()
        self.runtime=None;self.error=None;self.startup_error=None;self.stop_requested=False
        self.startup_token=CancellationToken();self.activated=threading.Event()
        self.startup_aborted=False;self.cleanup_failed=False
        self.thread=threading.Thread(target=self.run,name='teammate-'+member['name'],daemon=True)
    def notify(self):
        with self.condition:self.condition.notify_all()
    def stop(self):
        self.stop_requested=True
        self.startup_token.cancel()
        try:
            runtime=self.runtime
            if runtime is not None:
                if runtime.cancellation is not None:runtime.cancellation.cancel()
                runtime.client.cancel_active()
        finally:self.notify()
    def activate(self, token):
        with self.condition:
            if token is not None:token.check()
            self.startup_token.check()
            self.activated.set();self.condition.notify_all()
    def run(self):
        from ..agents.factory import AgentSessionFactory
        m=self.manager;reason=None;started_work=False
        try:
            member=m.member(self.agent_id)
            spec=TeammateSpec(m,self.agent_id,member['session_id'])
            self.runtime=AgentSessionFactory(m.lead,m.client_factory).create_teammate(
                spec,display=lambda k,v:m.display(self.agent_id,k,v))
            if self.stop_requested:return
            self.runtime.cancellation=self.startup_token
            self.startup_token.check()
            if self.task_id is not None:
                self.runtime.tasks.claim_host(self.task_id,adopt=True)
                started_work=True
            self.startup_token.check()
            self.started.set()
            # Readiness is not permission to run. The spawning thread confirms
            # cancellation, status persistence and its result before activation.
            with self.condition:
                while not self.activated.is_set() and not self.stop_requested:
                    self.condition.wait(timeout=.05)
            self.startup_token.check()
            self.runtime.cancellation=None
            while not self.stop_requested:
                m.boundary(self.runtime)
                if self.stop_requested:break
                member=m.member(self.agent_id)
                if member['plan_status']=='pending':
                    m.state(self.agent_id,'WAITING_APPROVAL')
                    with self.condition:self.condition.wait(timeout=2)
                    continue
                if member['plan_status'] not in {'pending'} and member['state']=='WAITING_APPROVAL':
                    m.state(self.agent_id,'WORKING');started_work=True
                rows=m.pending(self.runtime)
                if not started_work and not rows and self.runtime.tasks.assignment is None and member['auto_claim']:
                    from ..storage.task_store import view
                    graph=self.runtime.tasks.store.snapshot()
                    for task in sorted(graph.values(),key=lambda r:(r.created_at,r.task_id)):
                        if task.team_id!=m.team_id or task.auto_claim_blocked or not view(task,graph)['ready']:continue
                        try:self.runtime.tasks.claim_host(task.task_id,automatic=True)
                        except TaskError:continue
                        started_work=True;break
                if not started_work and not rows:
                    with self.condition:self.condition.wait(timeout=2)
                    continue
                m.state(self.agent_id,'WORKING')
                messages=m.input_messages(self.runtime,rows)
                if started_work:
                    assignment=self.runtime.tasks.assignment
                    task=self.runtime.tasks.store._get(self.runtime.tasks.store.snapshot(),assignment.task_id)
                    messages.append(Message(MessageRole.USER,[TextBlock('[HOST ASSIGNMENT; bounded team scope]\n'+json.dumps(task.to_dict(),ensure_ascii=False))],origin={'kind':'team'}))
                started_work=False
                outcome=m.execute(self.runtime,messages,rows)
                if self.stop_requested:break
                if outcome!=TurnOutcome.COMPLETED:
                    reason='队友执行失败或达到本轮限制：'+outcome.value
                    break
                member=m.member(self.agent_id)
                if member['plan_status']=='pending':
                    m.state(self.agent_id,'WAITING_APPROVAL');continue
                m.report(self.runtime)
                self.runtime.tasks.finished_batch=False
                m.state(self.agent_id,'WORKING' if self.runtime.tasks.assignment else 'IDLE')
                # Text alone does not complete tasks. Wait for actual new input.
        except BaseException as exc:
            if not (isinstance(exc,Cancelled) and self.stop_requested):
                self.error=exc;reason=f'{type(exc).__name__}: {exc}'
            if not self.started.is_set():self.startup_error=exc
        finally:
            failed=False
            try:
                m.state(self.agent_id,'STOPPING')
                if self.runtime is not None:
                    self.runtime.tasks.release_blocked=bool(reason) or self.startup_aborted
                    self.runtime.close()
            except BaseException as exc:
                failed=True;self.error=exc;self.cleanup_failed=True
            try:
                m.state(self.agent_id,'FAILED' if failed or reason else 'STOPPED',error=str(self.error)[:2000] if self.error else reason)
                if not failed:m.shutdown_ack(self.agent_id)
            except BaseException as exc:self.error=exc;failed=True;self.cleanup_failed=True
            self.started.set();self.stopped.set();m.wake_lead()


class TeamManager:
    def __init__(self,lead,client_factory):
        self.lead=lead;self.team_id=new_id('team');self.client_factory=client_factory
        self.store=TeamStore(lead.config.workspace,self.team_id);self.store.create(lead.context)
        self.events=EventStore(lead.config.workspace);self.workers={};self.semaphore=threading.BoundedSemaphore(2)
        self.console=queue.Queue(maxsize=256);self.closed=False
        self.clients_lock=threading.Lock();self.clients=[lead.client]

    def member(self,agent_id):
        try:return self.store.read()['members'][agent_id]
        except KeyError as exc:raise TaskError('未知队友实例。') from exc

    def resolve(self,name):
        if type(name) is not str or not name:raise TaskError('目标名称无效。')
        data=self.store.read()
        if name.lower()=='lead':return {'agent_id':data['lead_agent_id'],'session_id':data['lead_session_id'],'name':'lead','state':'WORKING'}
        member=next((m for m in data['members'].values() if m['name'].lower()==name.lower()),None)
        if member is None or member['state'] in {'STOPPED','FAILED','interrupted'}:raise TaskError('收件队友不存在或已停止。')
        return member

    def wake_lead(self):
        if self.lead.events is not None:self.lead.events.wakeup.notify()

    def display(self,agent_id,kind,value):
        if kind not in {'call','result','notice'}:return
        text=str(value)[:300]
        try:self.console.put_nowait(f'[{self.member(agent_id)["name"]}] {kind}: {text}')
        except queue.Full:pass
        self.wake_lead()

    def state(self,agent_id,state,*,error=None):
        changed=False
        with self.store.transaction() as data:
            member=data['members'][agent_id]
            if member['state']!=state:member['state']=state;changed=True
            if error:member['error']=error
        if changed:
            self.emit(agent_id,self.lead.context.agent_id,'status',state+((': '+error) if error else ''))

    def emit(self,sender,target,kind,content,request_id=None,*,event_id=None,created_at=None):
        data=self.store.read()
        target_session=data['lead_session_id'] if target==data['lead_agent_id'] else data['members'][target]['session_id']
        event_kind={'message':'team_message','status':'team_status','plan_request':'team_plan',
                    'plan_decision':'team_control','shutdown_request':'team_control','shutdown_response':'team_status'}[kind]
        event=RuntimeEvent(event_kind,target_session,{'team_id':self.team_id,'sender':sender,'target':target,'type':kind,'content':content[:8000],'request_id':request_id},
            source_agent_id=sender,source_id=self.team_id,**({'event_id':event_id,'created_at':created_at} if event_id else {}))
        # Re-materializing a saved request must preserve its original timestamp/envelope.
        if event_id is not None:
            try:
                old=self.events.get(event_id)
                return RuntimeEvent.from_dict(old['event'])
            except StorageError:
                if self.events.path(event_id).exists():raise
        self.events.put(event)
        if target==data['lead_agent_id']:self.wake_lead()
        elif target in self.workers:self.workers[target].notify()
        return event

    def validate_event(self,event,runtime):
        if event.source_id!=self.team_id:return False
        data=self.store.read();p=event.payload
        valid={data['lead_agent_id']:data['lead_session_id'],**{a:m['session_id'] for a,m in data['members'].items()}}
        return (p['sender'] in valid and valid.get(p['target'])==runtime.state.session_id and p['target']==runtime.context.agent_id
                and event.target_session_id==runtime.state.session_id)

    def spawn(self,args,context):
        if self.closed:raise TaskError('团队正在关闭。')
        if any(w.runtime is not None and w.runtime.cleanup_uncertain for w in self.workers.values()):
            raise TaskError('旧队友资源状态不确定，请先检查；不启动新执行者。')
        name=safe_name(args['name'])
        if name.lower() in {'lead','host','system'}:raise TaskError('名称为宿主保留。')
        with self.store.transaction() as data:
            if any(m['name'].lower()==name.lower() for m in data['members'].values()):raise TaskError('队友名已使用。')
            if sum(m['state'] not in {'STOPPED','FAILED','interrupted'} for m in data['members'].values())>=3:raise TaskError('最多 3 个活动队友。')
            member={'agent_id':new_id('agent'),'session_id':new_id('session'),'name':name,'state':'STARTING',
                'auto_claim':args['auto_claim'],'require_plan':args['require_plan'],'plan_status':'not_required',
                'plan_feedback':'','assignment_version':0,'plan_revision':0,'latest_plan':None,'task_id':None,'claim_id':None,'error':None,'shutdown_request':None}
            data['members'][member['agent_id']]=member
        worker=Worker(self,member,args.get('task_id'));self.workers[member['agent_id']]=worker
        try:
            worker.thread.start()
            # No task/team lock is held while waiting for startup or cleanup.
            deadline=time.monotonic()+STARTUP_TIMEOUT
            while not worker.started.wait(.05):
                if self.lead.cancellation is not None:self.lead.cancellation.check()
                if time.monotonic()>=deadline:raise TaskError('队友启动超时。')
            if self.lead.cancellation is not None:self.lead.cancellation.check()
            if worker.startup_error is not None:raise TaskError('队友启动失败：'+str(worker.startup_error))
            if worker.stopped.is_set():raise TaskError('队友在确认启动前已停止。')
            self.state(worker.agent_id,'WORKING' if args.get('task_id') is not None else 'IDLE')
            result=ToolOutput(json.dumps(self.member(worker.agent_id),ensure_ascii=False))
            worker.activate(self.lead.cancellation)
            return result
        except BaseException:
            with durable_section():self._abort_startup(worker)
            raise

    def _abort_startup(self,worker):
        worker.startup_aborted=True
        try:worker.stop()
        except Exception:pass  # The owner thread's confirmed cleanup is authoritative.
        error=None
        try:
            if worker.thread.ident is None:
                self.state(worker.agent_id,'FAILED',error='线程未能启动')
                worker.started.set();worker.stopped.set()
            else:
                with self.store.transaction() as data:
                    member=data['members'][worker.agent_id]
                    if member['state'] not in {'STOPPED','FAILED'}:member['state']='STOPPING'
        except Exception as exc:error=exc
        finally:
            if worker.thread.ident is not None:worker.thread.join(STARTUP_STOP_TIMEOUT)
        if (error is not None or worker.thread.is_alive() or worker.cleanup_failed
                or worker.runtime is not None and worker.runtime.cleanup_uncertain):
            self.lead.cleanup_uncertain=True
            raise StorageError('新队友启动已中止，但收尾未确认；保留任务认领并停止新工作。') from error

    @contextmanager
    def task_adoption(self, record):
        """Only an explicit spawn may adopt this lead's inactive previous team."""
        if record.team_id is None or record.team_id == self.team_id:
            yield None
            return
        lead = self.lead.context
        old = TeamStore(self.lead.config.workspace, record.team_id)
        snapshot = old.read()
        if (snapshot['lead_session_id'] != lead.session_id or snapshot['lead_agent_id'] != lead.agent_id
                or record.created_by['session_id'] != lead.session_id
                or record.created_by['agent_id'] != lead.agent_id):
            raise TaskError('旧团队任务不属于当前 lead 会话和身份，不能迁移。')
        if any(m['state'] not in {'STOPPED','FAILED','interrupted'} for m in snapshot['members'].values()):
            raise TaskError('旧团队仍有活动成员，不能迁移任务。')
        # Session lifecycle locks precede task/team locks. Hold them until the
        # new claim is committed; terminal metadata alone cannot prove exit.
        with ExitStack() as locks:
            for member in snapshot['members'].values():
                session = SessionStore(self.lead.config.workspace, member['session_id'])
                metadata = session._metadata()
                if (metadata['identity']['agent_id'] != member['agent_id']
                        or metadata['identity']['role'] != 'teammate'
                        or metadata['identity'].get('parent_agent_id') != lead.agent_id
                        or metadata.get('team_id') != record.team_id
                        or metadata.get('lead_session_id') != lead.session_id):
                    raise TaskError('旧队友会话来源不匹配，不能迁移任务。')
                path = session._path('session.lock')
                if not path.is_file():raise TaskError('旧队友会话锁缺失，无法确认停止。')
                try:lock = SessionLock(path)
                except StorageError as exc:raise TaskError('旧队友会话仍活动或锁不可用，不能迁移任务。') from exc
                locks.callback(lock.close)
            @contextmanager
            def authorize(current, graph):
                with file_lock(old.workspace, old.path('state.lock')):
                    if current.team_id != record.team_id or old.read() != snapshot:
                        raise TaskError('旧团队归属或状态已变化，请重新检查后派发。')
                    if (current.status != 'pending' or current.owner is not None
                            or current.created_by != record.created_by):
                        raise TaskError('仅可迁移当前 lead 的 pending、无人认领任务。')
                    if any(r.team_id == record.team_id and r.owner is not None for r in graph.values()):
                        raise TaskError('旧团队仍有遗留认领；请先检查旧资源并显式释放。')
                    yield
            yield authorize

    def assignment_changed(self,runtime,record):
        with self.store.transaction() as data:
            member=data['members'][runtime.context.agent_id]
            member.update(plan_feedback='',task_id=record.task_id if record else None,claim_id=record.owner['claim_id'] if record else None,
                assignment_version=member['assignment_version']+1,latest_plan=None,
                plan_status='required' if record and member['require_plan'] else 'not_required')
        runtime.skills.catalog=type(runtime.skills.catalog)()
        if record:
            runtime.skills.context=runtime.context;runtime.skills.refresh()

    def _current(self,runtime,member,graph):
        assignment=runtime.tasks.assignment
        if assignment is None:raise TaskError('队友当前没有 assignment。')
        record=runtime.tasks.store._get(graph,assignment.task_id)
        if record.owner!=assignment.owner or member['task_id']!=record.task_id or member['claim_id']!=assignment.claim_id:
            raise TaskError('任务、claim 或 assignment 版本已变化。')
        return record

    def request_plan(self,args,context):
        target=self.resolve(args['teammate']);worker=self.workers.get(target['agent_id'])
        if worker is None or worker.runtime is None:raise TaskError('队友未就绪。')
        with self.lead.tasks.store._locked():
            graph=self.lead.tasks.store._read_all()
            with self.store.transaction() as data:
                member=data['members'][target['agent_id']]
                record=self._current(worker.runtime,member,graph)
                if args['task_id']!=record.task_id:raise TaskError('任务不是队友当前 assignment。')
                member.update(plan_status='required',latest_plan=None,plan_revision=member['plan_revision']+1)
        worker.notify();return ToolOutput('已要求当前任务重新提交计划；旧批准失效。')

    def submit_plan(self,runtime,args,context):
        with runtime.tasks.store._locked():
            graph=runtime.tasks.store._read_all()
            with self.store.transaction() as data:
                member=data['members'][runtime.context.agent_id];record=self._current(runtime,member,graph)
                if member['plan_status']=='pending':raise TaskError('已有待审批计划。')
                request_id=new_id('request');revision=member['plan_revision']+1
                row={'schema_version':1,'request_id':request_id,'team_id':self.team_id,'type':'plan','status':'pending',
                    'sender':runtime.context.agent_id,'reviewer':data['lead_agent_id'],'task_id':record.task_id,'claim_id':record.owner['claim_id'],
                    'assignment_version':member['assignment_version'],'plan_revision':revision,'created_at':utc_now(),
                    'plan':args['plan'],'feedback':'','event_id':new_id('event'),'decision_event_id':new_id('event')}
                self.store.save_request(row)
                member.update(plan_status='pending',latest_plan=request_id,plan_revision=revision)
        self.sync_requests()
        return ToolOutput(json.dumps(row,ensure_ascii=False))

    def review_plan(self,args,context):
        with self.lead.tasks.store._locked():
            graph=self.lead.tasks.store._read_all()
            with self.store.transaction() as data:
                request_id=args['request_id']
                referenced=any(request_id in (m['latest_plan'],m['shutdown_request']) for m in data['members'].values())
                row=self.store.request(request_id,allow_missing=not referenced)
                if row is None:raise TaskError('当前团队没有该计划请求；旧团队请求或无效 ID 不能审批，请检查当前队友计划。')
                if row['type']!='plan' or row['reviewer']!=context.agent_id or row['status']!='pending':raise TaskError('非当前待审批计划或身份不匹配。')
                member=data['members'].get(row['sender']);worker=self.workers.get(row['sender'])
                if member is None or worker is None or worker.runtime is None:raise TaskError('队友实例已失效。')
                record=self._current(worker.runtime,member,graph)
                if (member['latest_plan']!=row['request_id'] or member['assignment_version']!=row['assignment_version'] or
                    member['plan_revision']!=row['plan_revision'] or record.task_id!=row['task_id'] or record.owner['claim_id']!=row['claim_id']):
                    raise TaskError('陈旧计划不能批准。')
                row.update(status='approved' if args['approve'] else 'rejected',feedback=args['feedback'])
                self.store.save_request(row)
                # Worker applies the authority at its next safe boundary.
        self.sync_requests();worker.notify();return ToolOutput(json.dumps(row,ensure_ascii=False))

    def request_shutdown(self,args,context):
        target=self.resolve(args['teammate']);agent_id=target['agent_id']
        if agent_id==self.lead.context.agent_id:raise TaskError('只能关闭队友。')
        with self.store.transaction() as data:
            member=data['members'][agent_id]
            if member['shutdown_request']:row=self.store.request(member['shutdown_request'])
            else:
                row={'schema_version':1,'request_id':new_id('request'),'team_id':self.team_id,'type':'shutdown','status':'pending',
                    'sender':data['lead_agent_id'],'reviewer':agent_id,'created_at':utc_now(),'event_id':new_id('event'),'response_event_id':new_id('event')}
                self.store.save_request(row);member['shutdown_request']=row['request_id']
        self.sync_requests();self.workers[agent_id].stop()
        return ToolOutput(json.dumps(row,ensure_ascii=False)+'\n停止请求已保存，尚不代表实际收尾完成。')

    def sync_requests(self):
        data=self.store.read()
        for row in self.store.requests():
            if row['type']=='plan':
                member=data['members'].get(row['sender'])
                if member is None:continue
                if member['latest_plan']==row['request_id']:
                    self.emit(row['sender'],row['reviewer'],'plan_request',row['plan'],row['request_id'],event_id=row['event_id'],created_at=row['created_at'])
                    if row['status'] in {'approved','rejected'}:
                        self.emit(row['reviewer'],row['sender'],'plan_decision',row['status']+': '+row['feedback'],row['request_id'],event_id=row['decision_event_id'],created_at=row['created_at'])
            elif row['status'] in {'pending','applied'}:
                self.emit(row['sender'],row['reviewer'],'shutdown_request','stop requested',row['request_id'],event_id=row['event_id'],created_at=row['created_at'])
            elif row['status']=='stopped':
                self.emit(row['reviewer'],row['sender'],'shutdown_response','stopped after cleanup',row['request_id'],event_id=row['response_event_id'],created_at=row['created_at'])

    def boundary(self,runtime):
        if runtime is self.lead:
            while True:
                try:self.lead.display('notice',self.console.get_nowait())
                except queue.Empty:break
        self.sync_requests()
        if runtime is self.lead:return
        worker=self.workers[runtime.context.agent_id]
        for eventrow in self.events.list(runtime.state.session_id):
            if eventrow['status']!='pending':continue
            event=RuntimeEvent.from_dict(eventrow['event'])
            if event.kind!='team_control':continue
            if not self.validate_event(event,runtime):raise StorageError('控制事件身份不匹配。')
            p=event.payload;request=self.store.request(p['request_id'])
            if p['type']=='shutdown_request':
                if (request['type']!='shutdown' or request['reviewer']!=runtime.context.agent_id or request['sender']!=self.lead.context.agent_id
                    or p['sender']!=request['sender'] or p['target']!=request['reviewer']):
                    raise StorageError('关闭请求来源无效。')
                with self.store.transaction() as data:
                    if data['members'][runtime.context.agent_id]['shutdown_request']!=request['request_id']:raise StorageError('关闭请求版本无效。')
                    data['members'][runtime.context.agent_id]['state']='STOPPING'
                    if request['status']=='pending':request['status']='applied';self.store.save_request(request)
                self.events.host_accept(event.event_id);worker.stop()
            elif p['type']=='plan_decision':
                with runtime.tasks.store._locked():
                    graph=runtime.tasks.store._read_all()
                    with self.store.transaction() as data:
                        member=data['members'][runtime.context.agent_id]
                        valid=(request['type']=='plan' and request['reviewer']==self.lead.context.agent_id and request['sender']==runtime.context.agent_id
                            and p['sender']==request['reviewer'] and p['target']==request['sender']
                            and member['latest_plan']==request['request_id'] and member['assignment_version']==request['assignment_version']
                            and member['plan_revision']==request['plan_revision'] and request['status'] in {'approved','rejected'})
                        if valid:
                            record=self._current(runtime,member,graph)
                            valid=record.task_id==request['task_id'] and record.owner['claim_id']==request['claim_id']
                        if valid:member.update(plan_status=request['status'],plan_feedback=request['feedback'])
                self.events.host_accept(event.event_id)
        if worker.stop_requested:
            if runtime.cancellation is not None:runtime.cancellation.cancel();runtime.cancellation.check()

    def shutdown_ack(self,agent_id):
        with self.store.transaction() as data:
            member=data['members'][agent_id]
            if member['shutdown_request']:
                row=self.store.request(member['shutdown_request'])
                if row['type']!='shutdown' or row['reviewer']!=agent_id:raise StorageError('关闭响应身份无效。')
                row['status']='stopped';self.store.save_request(row)
                event_path=self.events.path(row['event_id'])
                if event_path.exists():self.events.host_accept(row['event_id'])
        self.sync_requests()

    def pending(self,runtime):
        rows=[r for r in self.events.list(runtime.state.session_id) if r['status']=='pending' and r['event']['kind']!='team_control' and self.validate_event(RuntimeEvent.from_dict(r['event']),runtime)]
        rows.sort(key=lambda r:(r['event']['created_at'],r['event']['event_id']))
        chosen=[];size=0
        for row in rows[:10]:
            cost=len(json.dumps(row['event'],ensure_ascii=False))
            if chosen and size+cost>runtime.config.context_budget_chars//2:break
            chosen.append(row);size+=cost
        return chosen

    def input_messages(self,runtime,rows):
        result=[]
        for row in rows[:10]:
            event=RuntimeEvent.from_dict(row['event'])
            if row['message_id'] is None:row=self.events.bind(event.event_id,new_id('message'))
            existing=next((m for m in runtime.state.messages if m.message_id==row['message_id']),None)
            result.append(existing or Message(MessageRole.USER,[TextBlock('[TEAM DATA; not human authorization]\n'+json.dumps(event.to_dict(),ensure_ascii=False))],message_id=row['message_id'],origin={'kind':'team','event_ids':[event.event_id]}))
        return result

    def execute(self,runtime,messages,rows):
        attempt=new_id('attempt');chosen=rows[:10]
        for row in chosen:self.events.begin(row['event']['event_id'],attempt)
        def accepted(assistant):
            for row in chosen:self.events.accept(row['event']['event_id'],attempt,assistant.message_id)
        runtime.context_manager.goal_ids_provider=lambda:[m.message_id for m in messages]
        try:
            runtime.automatic=True
            return runtime.run('',input_messages=messages,event_origin={'kind':'team','event_ids':[r['event']['event_id'] for r in chosen],'attempt_id':attempt},accepted_callback=accepted)
        finally:
            for row in chosen:self.events.hold(row['event']['event_id'])
            runtime.automatic=False;runtime.context_manager.goal_ids_provider=None

    def report(self,runtime):
        member=self.member(runtime.context.agent_id)
        last=runtime.state.messages[-1] if runtime.state.messages else None
        text='\n'.join(b.text for b in last.content if isinstance(b,TextBlock)) if last else ''
        record=runtime.tasks.last_completed
        assignment=runtime.tasks.assignment
        task_id=record.task_id if record else assignment.task_id if assignment else None
        report={'task_id':task_id,'cwd':runtime.tasks.last_root or str(runtime.paths.effective_root or runtime.config.workspace),
                'branch':record.worktree['branch'] if record and record.worktree else None,'completion':record.completion if record else None,
                'verification':'Refer to actual teammate tool results; lead verification required.',
                'integration':'Changes retained in checkout; not automatically committed or merged.','report':text[:4000]}
        if record and record.worktree:
            service=runtime.tasks.worktrees;path=record.worktree['path']
            try:report['git_status']=service.git('status','--porcelain','--untracked-files=all',cwd=path)[:2000]
            except TaskError:report['git_status']='unavailable; inspect checkout'
        self.emit(runtime.context.agent_id,self.lead.context.agent_id,'status',json.dumps(report,ensure_ascii=False))
        runtime.tasks.last_completed=None

    def guard(self,runtime,name,context):
        if runtime is self.lead:return
        member=self.member(runtime.context.agent_id)
        if member['state'] in {'STOPPING','STOPPED','FAILED','interrupted'} and name in PROJECT|{'claim_task','complete_task','submit_plan'}:
            raise PermissionError('队友正在停止，不能开始新工作。')
        if runtime.tasks.assignment is None and name in PROJECT|{'complete_task','submit_plan'}:raise PermissionError('无 assignment 的队友没有项目操作权限。')
        if name in SIDE_EFFECTS and member['plan_status'] in {'required','pending','rejected'}:raise PermissionError('当前计划尚未批准；仅允许只读检查和重新提交计划。')

    def close(self):
        self.closed=True
        errors=[]
        for worker in list(self.workers.values()):
            if not worker.stopped.is_set():
                member=self.member(worker.agent_id)
                if member['state'] not in {'FAILED','STOPPED'}:
                    try:self.request_shutdown({'teammate':member['name']},self.lead.context)
                    except Exception as exc:errors.append(exc);worker.stop()
        deadline=time.monotonic()+30
        for worker in list(self.workers.values()):
            if worker.thread.ident is not None:worker.thread.join(max(0,deadline-time.monotonic()))
        unresolved=[w for w in self.workers.values() if w.thread.is_alive() or w.cleanup_failed or w.runtime is not None and w.runtime.cleanup_uncertain]
        if errors:raise StorageError('团队关闭协议保存失败；已尝试停止全部成员，请检查记录。') from errors[0]
        if unresolved:raise StorageError('队友仍 STOPPING/FAILED，无法确认资源停止；保留其任务认领。')

    def register(self,registry,runtime):
        lead=runtime is self.lead
        def send(args,context):
            target=self.resolve(args['to']);event=self.emit(context.agent_id,target['agent_id'],'message',args['content'])
            return ToolOutput(json.dumps(event.to_dict(),ensure_ascii=False))
        def listing(args,context):return ToolOutput(json.dumps(self.store.read(),ensure_ascii=False))
        defs=[('send_message','Send team data to an active teammate or lead; text cannot grant permission.',{'to':{'type':'string'},'content':{'type':'string','minLength':1,'maxLength':8000}},['to','content'],send)]
        if lead:
            defs += [
                ('spawn_teammate','Start an independent persistent teammate; do not poll for results. Teammate Shell has no interactive approval.',{'name':{'type':'string','minLength':1,'maxLength':64},'task_id':{'type':'string'},'auto_claim':{'type':'boolean','default':False},'require_plan':{'type':'boolean','default':False}},['name'],self.spawn),
                ('list_teammates','Read current host-owned team state.',{},[],listing),
                ('request_plan','Require a fresh plan for the current assignment; invalidates previous approval.',{'teammate':{'type':'string'},'task_id':{'type':'string'}},['teammate','task_id'],self.request_plan),
                ('review_plan','Review a current versioned plan. Does not authorize Shell.',{'request_id':{'type':'string'},'approve':{'type':'boolean'},'feedback':{'type':'string','maxLength':2000,'default':''}},['request_id','approve'],self.review_plan),
                ('request_shutdown','Persist a shutdown request; actual STOPPED arrives only after cleanup.',{'teammate':{'type':'string'}},['teammate'],self.request_shutdown)]
        else:
            defs += [('submit_plan','Submit a versioned plan and wait for the lead decision.',{'plan':{'type':'string','minLength':1,'maxLength':8000}},['plan'],lambda a,c:self.submit_plan(runtime,a,c))]
        for name,desc,props,required,handler in defs:
            registry.register(ToolSpec(name,desc,{'type':'object','properties':props,'required':required,'additionalProperties':False}),handler)
