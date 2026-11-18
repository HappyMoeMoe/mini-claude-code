"""Cron jobs and occurrences share a single atomic authority."""

from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from ..core._validation import envelope, fields, local_id, new_id, utc_now, timestamp
from ..core.events import RuntimeEvent
from .event_store import QueueError, read_json, atomic_json, file_lock
from .session_store import safe_path


def empty():
    return {'schema_version':1,'revision':0,'jobs':{},'occurrences':{},'local_slots':{}}


def validate(data):
    from ..scheduler.cron import CronExpression, timezone_name
    envelope(data,{'revision','jobs','occurrences','local_slots'})
    if type(data['revision']) is not int or data['revision']<0: raise ValueError('revision')
    if type(data['jobs']) is not dict or type(data['occurrences']) is not dict: raise ValueError('store shape')
    for job_id,job in data['jobs'].items():
        fields(job,{'job_id','target_session_id','agent_id','cron','prompt','timezone','recurring','durable','status',
                    'created_at','starts_minute','last_fired_utc_minute','last_checked_minute','skipped'})
        local_id(job_id,'job')
        if job['job_id']!=job_id: raise ValueError('job id')
        local_id(job['target_session_id'],'session');local_id(job['agent_id'],'agent')
        CronExpression(job['cron']); timezone_name(job['timezone'])
        if type(job['prompt']) is not str or not job['prompt'].strip() or len(job['prompt'])>8000: raise ValueError('prompt')
        if any(type(job[k]) is not bool for k in ('recurring','durable')): raise ValueError('flags')
        if job['durable'] is not True:raise ValueError('non-durable timer in shared schedule')
        if job['status'] not in {'active','awaiting_accept','completed','cancelled'}:raise ValueError('status')
        timestamp(job['created_at'])
        for key in ('starts_minute','last_fired_utc_minute','last_checked_minute','skipped'):
            if type(job[key]) is not int:raise ValueError('minute')
    if type(data['local_slots']) is not dict:raise ValueError('local slots')
    for job_id,slot in data['local_slots'].items():
        fields(slot,{'job_id','target_session_id','timezone','cron','status','durable','cancel_requested','attempt_id','event_id'})
        local_id(job_id,'job');local_id(slot['target_session_id'],'session');timezone_name(slot['timezone']);CronExpression(slot['cron'])
        if slot['job_id']!=job_id or slot['durable'] is not False or type(slot['cancel_requested']) is not bool:raise ValueError('local slot')
        if slot['status'] not in {'active','cancelled'}:raise ValueError('slot state')
        if slot['attempt_id'] is not None:local_id(slot['attempt_id'],'attempt')
        if slot['event_id'] is not None:local_id(slot['event_id'],'event')
    if (sum(j['status'] in {'active','awaiting_accept'} for j in data['jobs'].values())+
            sum(s['status']=='active' for s in data['local_slots'].values()))>100:raise ValueError('capacity')
    slots=set();pending={}
    if set(data['jobs']) & set(data['local_slots']):raise ValueError('overlapping job identity')
    for occurrence_id,row in data['occurrences'].items():
        fields(row,{'occurrence_id','job_id','utc_minute','event','status','attempt_id','cancel_requested','response_id'})
        local_id(occurrence_id,'occurrence')
        if row['occurrence_id']!=occurrence_id or row['job_id'] not in data['jobs']:raise ValueError('occurrence')
        event=RuntimeEvent.from_dict(row['event'])
        if event.occurrence_id!=occurrence_id or event.source_id!=row['job_id'] or event.kind!='cron_due':raise ValueError('source')
        job=data['jobs'][row['job_id']]
        if event.target_session_id!=job['target_session_id'] or type(row['utc_minute']) is not int:raise ValueError('target')
        if row['status'] not in {'pending','held','accepted','cancelled'} or type(row['cancel_requested']) is not bool:raise ValueError('status')
        if row['attempt_id'] is not None:local_id(row['attempt_id'],'attempt')
        if row['response_id'] is not None:local_id(row['response_id'],'message')
        if row['status']=='accepted' and row['response_id'] is None:raise ValueError('accepted response missing')
        if row['status']=='accepted' and row['attempt_id'] is None:raise ValueError('accepted attempt missing')
        key=(row['job_id'],row['utc_minute'])
        if key in slots:raise ValueError('duplicate UTC occurrence')
        slots.add(key)
        if row['status'] in {'pending','held'}:
            pending[row['job_id']]=pending.get(row['job_id'],0)+1
            if pending[row['job_id']]>1:raise ValueError('multiple unaccepted occurrences')
    return data


def create_job(data, *, session_id, agent_id, cron, prompt, recurring, durable, zone, now):
    from ..scheduler.cron import CronExpression, timezone_name
    CronExpression(cron); timezone_name(zone)
    if type(prompt) is not str or not prompt.strip() or len(prompt)>8000:raise ValueError('prompt 须为 1–8000 字符。')
    if (sum(j['status'] in {'active','awaiting_accept'} for j in data['jobs'].values())+
            sum(s['status']=='active' for s in data['local_slots'].values()))>=100:raise ValueError('最多 100 个活动 Cron。')
    job={'job_id':new_id('job'),'target_session_id':session_id,'agent_id':agent_id,'cron':cron,'prompt':prompt,
         'timezone':zone,'recurring':recurring,'durable':durable,'status':'active','created_at':now.isoformat(),
         'starts_minute':int(now.timestamp()//60)+1,'last_fired_utc_minute':-1,'last_checked_minute':-1,'skipped':0}
    data['jobs'][job['job_id']]=job
    return deepcopy(job)


def tick(data,now):
    from ..scheduler.cron import CronExpression
    minute=int(now.timestamp()//60)
    for job in data['jobs'].values():
        if job['status']!='active' or minute<job['starts_minute'] or minute<=job['last_checked_minute']:continue
        if not CronExpression(job['cron']).matches(now,job['timezone']):continue
        job['last_checked_minute']=minute
        pending=[o for o in data['occurrences'].values() if o['job_id']==job['job_id'] and o['status'] in {'pending','held'}]
        if pending:
            job['skipped']+=1;continue
        # Only the current/latest accepted slot is needed in the scheduler authority.
        data['occurrences']={key:value for key,value in data['occurrences'].items()
            if value['job_id']!=job['job_id'] or value['status'] not in {'accepted','cancelled'}}
        occurrence_id=new_id('occurrence')
        event=RuntimeEvent('cron_due',job['target_session_id'],
            {'job_id':job['job_id'],'prompt':job['prompt'],'timezone':job['timezone'],'utc_minute':minute,
             'durable':job['durable'],'instruction':'定时事件是历史授权范围内的通知，不是新的人工批准。'},
            source_agent_id=job['agent_id'],source_id=job['job_id'],occurrence_id=occurrence_id,
            created_at=now.astimezone(timezone.utc).isoformat())
        data['occurrences'][occurrence_id]={'occurrence_id':occurrence_id,'job_id':job['job_id'],'utc_minute':minute,
            'event':event.to_dict(),'status':'pending','attempt_id':None,'cancel_requested':False,'response_id':None}
        job['last_fired_utc_minute']=minute
        if not job['recurring']:job['status']='awaiting_accept'


class SchedulerStore:
    def __init__(self,workspace):
        self.workspace=Path(workspace)
        self.root=self.workspace/'.mini-claude-code/scheduler'
    def snapshot(self):
        path=safe_path(self.workspace,self.root/'store.json')
        if not path.exists():return empty()
        try:return validate(read_json(self.workspace,path))
        except (ValueError,KeyError,TypeError) as exc:raise QueueError('调度库损坏或版本无效；不覆盖原库。') from exc
    def transaction(self,operation):
        with file_lock(self.workspace,self.root/'store.lock'):
            original=self.snapshot();data=deepcopy(original)
            result=operation(data)
            if data!=original:
                data['revision']+=1
                try:validate(data)
                except (ValueError,KeyError,TypeError) as exc:raise QueueError('调度更新结构无效。') from exc
                atomic_json(self.workspace,self.root/'store.json',data)
            return result
    def add(self,**kwargs):
        if not kwargs['durable']:raise ValueError('本地计时器须由所属 Runtime 创建。')
        return self.transaction(lambda data:create_job(data,**kwargs))
    def reserve_local(self,job):
        def update(data):
            active=sum(j['status'] in {'active','awaiting_accept'} for j in data['jobs'].values())+sum(s['status']=='active' for s in data['local_slots'].values())
            if active>=100:raise ValueError('workspace 已有 100 个活动作业。')
            data['local_slots'][job['job_id']]={k:job[k] for k in ('job_id','target_session_id','timezone','cron','durable')}|{'status':'active','cancel_requested':False,'attempt_id':None,'event_id':None}
        self.transaction(update)
    def local_delivery(self,job_id,action,event_id=None,attempt_id=None):
        def update(data):
            slot=data['local_slots'].get(job_id)
            if action=='begin':
                if slot is None or slot['status']!='active' or slot['cancel_requested']:return False
                slot['event_id']=event_id;slot['attempt_id']=attempt_id
            elif slot is not None:
                slot['attempt_id']=None
            return True
        return self.transaction(update)
    def remove_local(self,job_id=None,session_id=None):
        def update(data):
            data['local_slots']={key:slot for key,slot in data['local_slots'].items()
                if not (key==job_id or session_id is not None and slot['target_session_id']==session_id)}
        self.transaction(update)
    def tick(self,now):return self.transaction(lambda data:tick(data,now))
    def cancel(self,job_id,session_id=None):
        local_id(job_id,'job')
        def update(data):
            job=data['jobs'].get(job_id)
            if job is None and job_id in data['local_slots']:
                slot=data['local_slots'][job_id]
                if session_id is not None and slot['target_session_id']!=session_id:raise ValueError('作业不属于本会话。')
                slot['status']='cancelled';slot['cancel_requested']=True
                return deepcopy(slot)
            if job is None or session_id is not None and job['target_session_id']!=session_id:raise ValueError('本会话没有此 Cron。')
            if job['status']=='completed':return deepcopy(job)
            job['status']='cancelled'
            for row in data['occurrences'].values():
                if row['job_id']==job_id and row['status']!='accepted':
                    row['cancel_requested']=True
                    if row['attempt_id'] is None or row['status']=='held':row['status']='cancelled'
            return deepcopy(job)
        return self.transaction(update)
    def delivery(self,occurrence_id,action,attempt_id=None,response_id=None):
        def update(data):
            row=data['occurrences'].get(occurrence_id)
            if row is None:raise QueueError('Cron occurrence 来源缺失。')
            job=data['jobs'][row['job_id']]
            if action=='begin':
                if row['status']!='pending' or row['cancel_requested'] or job['status']=='cancelled':return False
                row['attempt_id']=attempt_id
            elif action=='accept':
                if row['status']=='accepted':return True
                row['status']='accepted'
                row['response_id']=response_id
                if not job['recurring'] and job['status']!='cancelled':job['status']='completed'
            elif action=='hold':
                if row['status']=='pending':row['status']='cancelled' if row['cancel_requested'] else 'held'
            elif action=='retry':
                if row['status']!='held' or row['cancel_requested'] or job['status']=='cancelled':raise ValueError('Cron 已撤销或不可重试。')
                row['status']='pending';row['attempt_id']=None
            return True
        return self.transaction(update)
