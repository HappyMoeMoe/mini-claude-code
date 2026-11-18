"""Five-field numeric Cron, UTC slots, and one durable leader per workspace."""

from copy import deepcopy
from datetime import datetime,timezone
import fcntl
import os
import re
import threading
from zoneinfo import ZoneInfo,ZoneInfoNotFoundError

from ..core.events import RuntimeEvent
from ..core.messages import ToolSpec
from ..storage.event_store import QueueError
from ..storage.session_store import safe_path
from ..tools.registry import ToolOutput
import json


def timezone_name(value):
    if type(value) is not str:raise ValueError('timezone 必须为 IANA 时区。')
    try:ZoneInfo(value)
    except (ZoneInfoNotFoundError,ValueError) as exc:raise ValueError('无效 IANA 时区。') from exc
    return value


class CronExpression:
    def __init__(self,text):
        if type(text) is not str or len(text)>200:raise ValueError('Cron 须为五字段数值表达式。')
        self.parts=text.split()
        if len(self.parts)!=5:raise ValueError('Cron 必须有五字段：分 时 日 月 星期。')
        self.values=[]
        for part,(low,high) in zip(self.parts,[(0,59),(0,23),(1,31),(1,12),(0,6)]):
            values=set()
            for item in part.split(','):
                if item=='*':values.update(range(low,high+1))
                elif re.fullmatch(r'\*/[0-9]+',item):
                    step=int(item[2:])
                    if not 1<=step<=high-low+1:raise ValueError('Cron 步长超出字段范围。')
                    values.update(range(low,high+1,step))
                elif re.fullmatch(r'[0-9]+(?:-[0-9]+)?',item):
                    pair=item.split('-');start=int(pair[0]);end=int(pair[-1])
                    if not low<=start<=end<=high:raise ValueError('Cron 数值或区间无效。')
                    values.update(range(start,end+1))
                else:raise ValueError('不支持的 Cron 语法；仅支持数值、*、*/n、列表和闭区间。')
            self.values.append(values)
    def matches(self,now,zone):
        local=now.astimezone(ZoneInfo(zone))
        minute,hour,day,month,weekday=self.values
        dom=local.day in day;dow=(local.weekday()+1)%7 in weekday
        date_match=(dom or dow) if self.parts[2]!='*' and self.parts[4]!='*' else (dom and dow)
        return local.minute in minute and local.hour in hour and local.month in month and date_match


class CronService:
    def __init__(self,runtime,events,wake,clock=None):
        from ..storage.scheduler_store import SchedulerStore,empty
        self.runtime,self.events,self.wake=runtime,events,wake
        self.store=SchedulerStore(runtime.config.workspace)
        self.clock=clock or (lambda:datetime.now(timezone.utc))
        self.local=empty()
        self.lock=threading.RLock()
        self.stop_signal=threading.Event()
        self.thread=None;self.leader=None;self.error=None;self.seen=set()
    def start(self):
        if self.thread is None:
            self.thread=threading.Thread(target=self._run,name='mini-cc-cron',daemon=True);self.thread.start()
    def _run(self):
        while not self.stop_signal.is_set():
            try:self.tick_once()
            except Exception as exc:
                self.error=exc;self.wake();return
            self.stop_signal.wait(1)
    def _leader(self):
        if self.leader is not None:return True
        root=self.store.root
        safe_path(self.store.workspace,root/'leader.lock')
        root.mkdir(parents=True,exist_ok=True,mode=0o700)
        fd=os.open(root/'leader.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:os.close(fd);return False
        self.leader=fd;return True
    def tick_once(self,now=None):
        from ..storage.scheduler_store import tick
        now=now or self.clock()
        if now.tzinfo is None:raise ValueError('Cron clock must be timezone-aware')
        now=now.astimezone(timezone.utc)
        with self.lock:
            if self._leader():
                # Avoid a write transaction when there are no active durable jobs.
                snap=self.store.snapshot()
                if any(j['status']=='active' for j in snap['jobs'].values()):self.store.tick(now)
            tick(self.local,now)
            self.sync()
            pending={r['event']['event_id'] for r in self.events.list(self.runtime.state.session_id) if r['status']=='pending'}
            if pending-self.seen:self.wake()
            self.seen=pending
    def sync(self):
        with self.lock:
            durable=self.store.snapshot()
            for job in self.local['jobs'].values():
                slot=durable['local_slots'].get(job['job_id'])
                if job['status'] in {'active','awaiting_accept'} and (slot is None or slot['cancel_requested']):
                    job['status']='cancelled'
                    for row in self.local['occurrences'].values():
                        if row['job_id']==job['job_id'] and row['status']!='accepted':
                            row['cancel_requested']=True
                            if row['attempt_id'] is None or row['status']=='held':row['status']='cancelled'
            for data in (durable,self.local):
                for row in data['occurrences'].values():
                    event=RuntimeEvent.from_dict(row['event'])
                    self.events.put(event,held=row['status']=='held')
                    if row['status']=='accepted':
                        def accepted(copy):
                            copy.update(status='accepted',attempt_id=row['attempt_id'],response_id=row['response_id'])
                        self.events.change(event.event_id,accepted)
                    if row['cancel_requested'] or row['status']=='cancelled':
                        self.events.cancel(event.event_id,in_flight=row['attempt_id'] is not None and row['status']=='pending')
    def _local_delivery(self,occurrence_id,action,attempt_id,response_id):
        row=self.local['occurrences'].get(occurrence_id)
        if row is None:raise ValueError('本地 Cron 已随 Runtime 结束。')
        job=self.local['jobs'][row['job_id']]
        if action=='begin':
            if row['status']!='pending' or row['cancel_requested']:return False
            if not self.store.local_delivery(job['job_id'],'begin',row['event']['event_id'],attempt_id):return False
            row['attempt_id']=attempt_id
        elif action=='accept':
            if row['status']=='accepted':return True
            row['status']='accepted';row['response_id']=response_id
            if not job['recurring']:
                job['status']='completed';self.store.remove_local(job_id=job['job_id'])
            else:self.store.local_delivery(job['job_id'],'accept')
        elif action=='hold':
            if row['status']=='pending':row['status']='cancelled' if row['cancel_requested'] else 'held'
            self.store.local_delivery(job['job_id'],'hold')
        elif action=='retry':
            if row['status']!='held' or row['cancel_requested']:raise ValueError('本地 Cron 已取消。')
            row['status']='pending';row['attempt_id']=None
        return True
    def delivery(self,event,action,attempt_id=None,response_id=None):
        with self.lock:
            if event.payload['durable']:return self.store.delivery(event.occurrence_id,action,attempt_id,response_id)
            return self._local_delivery(event.occurrence_id,action,attempt_id,response_id)
    def schedule(self,args,context):
        from ..storage.scheduler_store import create_job
        if self.error is not None:raise ValueError('调度服务已停用，请检查库。')
        with self.lock:
            shared=self.store.snapshot()
            active=sum(j['status'] in {'active','awaiting_accept'} for j in shared['jobs'].values())+sum(s['status']=='active' for s in shared['local_slots'].values())
            if active>=100:raise ValueError('最多 100 个活动作业。')
            kwargs=dict(session_id=context.session_id,agent_id=context.agent_id,cron=args['cron'],prompt=args['prompt'],
                recurring=args['recurring'],durable=args['durable'],zone=args.get('timezone',self.runtime.config.timezone),now=self.clock().astimezone(timezone.utc))
            job=self.store.add(**kwargs) if args['durable'] else create_job(self.local,**kwargs)
            if not args['durable']:
                try:self.store.reserve_local(job)
                except BaseException:
                    self.local['jobs'].pop(job['job_id'],None);raise
            self.wake()
            return ToolOutput(json.dumps(job,ensure_ascii=False),metadata={'job_id':job['job_id'],'timezone':job['timezone']})
    def list(self,args,context):
        with self.lock:
            snapshots=(self.store.snapshot(),deepcopy(self.local))
            rows=[]
            for data in snapshots:
                for job in data['jobs'].values():
                    if job['target_session_id']==context.session_id:
                        pending=[{'event_id':o['event']['event_id'],'status':o['status']} for o in data['occurrences'].values()
                                 if o['job_id']==job['job_id'] and o['status'] in {'pending','held'}]
                        rows.append({**job,'unaccepted':pending})
            offset,limit=args['offset'],args['limit']
            return ToolOutput(json.dumps({'jobs':rows[offset:offset+limit],'total':len(rows)},ensure_ascii=False))
    def cancel(self,args,context):
        with self.lock:
            job_id=args['job_id']
            if job_id in self.local['jobs']:
                job=self.local['jobs'][job_id]
                if job['target_session_id']!=context.session_id:raise ValueError('作业不属于本会话。')
                if job['status']=='completed':return ToolOutput(json.dumps(job,ensure_ascii=False))
                self.store.cancel(job_id,context.session_id)
                job['status']='cancelled'
                for row in self.local['occurrences'].values():
                    if row['job_id']==job_id and row['status']!='accepted':
                        row['cancel_requested']=True
                        if row['attempt_id'] is None or row['status']=='held':row['status']='cancelled'
            else:job=self.store.cancel(job_id,context.session_id)
            self.sync();self.wake()
            return ToolOutput(json.dumps(job,ensure_ascii=False))
    def close(self):
        self.stop_signal.set()
        if self.thread is not None:self.thread.join(timeout=3)
        if self.thread is not None and self.thread.is_alive():raise QueueError('Cron 线程未确认停止。')
        with self.lock:
            for job in self.local['jobs'].values():job['status']='cancelled'
            for row in self.local['occurrences'].values():
                if row['status']!='accepted':
                    row['cancel_requested']=True;row['status']='cancelled'
                    self.events.cancel(row['event']['event_id'])
            for job_id in self.local['jobs']:self.store.remove_local(job_id=job_id)
            if self.leader is not None:os.close(self.leader);self.leader=None
    def register(self,registry):
        definitions=[
            ('schedule_cron','Schedule five-field numeric Cron for this session; day and weekday specified together use OR. Starts next UTC minute.',
             {'cron':{'type':'string','minLength':1,'maxLength':200},'prompt':{'type':'string','minLength':1,'maxLength':8000},
              'recurring':{'type':'boolean','default':True},'durable':{'type':'boolean','default':True},
              'timezone':{'type':'string','minLength':1}},['cron','prompt'],self.schedule),
            ('list_crons','List this session Cron jobs, including held occurrences that block later triggers.',
             {'offset':{'type':'integer','minimum':0,'default':0},'limit':{'type':'integer','minimum':1,'maximum':100,'default':50}},[],self.list),
            ('cancel_cron','Cancel this session job and unaccepted occurrences; does not roll back in-flight work.',
             {'job_id':{'type':'string','minLength':1}},['job_id'],self.cancel)]
        for name,description,props,required,handler in definitions:
            registry.register(ToolSpec(name,description,{'type':'object','properties':props,'required':required,'additionalProperties':False}),handler)
