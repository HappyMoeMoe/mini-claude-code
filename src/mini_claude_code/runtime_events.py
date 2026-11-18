"""Single-writer delivery, wake signals and explicit held-event recovery."""

import os
import json

from .core._validation import new_id,local_id
from .core.events import RuntimeEvent
from .core.messages import Message,MessageRole,TextBlock
from .storage.event_store import EventStore,QueueError
from .background.manager import BackgroundManager
from .scheduler.cron import CronService

AUTO_TOOLS=frozenset({'bash','read_file','write_file','edit_file','glob','load_skill','read_tool_output','compact',
                      'list_tasks','get_task','complete_task','list_crons','cancel_cron'})


class Wakeup:
    def __init__(self):self.read_fd,self.write_fd=os.pipe2(os.O_NONBLOCK|os.O_CLOEXEC);self.closed=False
    def notify(self):
        if self.closed:return
        try:os.write(self.write_fd,b'.')
        except OSError:
            if not self.closed:pass
    def drain(self):
        if self.closed:return
        try:
            while os.read(self.read_fd,4096):pass
        except BlockingIOError:pass
    def close(self):
        if not self.closed:
            self.closed=True;os.close(self.read_fd);os.close(self.write_fd)


class RuntimeEvents:
    def __init__(self,runtime):
        self.runtime=runtime
        self.wakeup=Wakeup()
        self.store=EventStore(runtime.config.workspace)
        self.background=BackgroundManager(runtime,self.store,self.wakeup.notify)
        self.cron=CronService(runtime,self.store,self.wakeup.notify)
        self.error=None
        self.goal_ids=[]
        self.active_event_ids=[]
        self.started=False
    def start(self):
        if not self.started:
            self.started=True
            if self.error is None and "scheduler" not in self.runtime.config.disabled_features:self.cron.start()
    def _source(self,event,action,attempt_id=None,response_id=None):
        if event.kind.startswith('team_'):
            return self.runtime.team is not None and self.runtime.team.validate_event(event, self.runtime)
        if event.kind=='cron_due':
            if 'scheduler' in self.runtime.config.disabled_features:return False
            if event.payload.get('durable') and action=='accept':
                data=self.cron.store.snapshot()
                if event.occurrence_id not in data['occurrences']:
                    job=data['jobs'].get(event.source_id)
                    if job is not None and job['recurring'] and job['last_fired_utc_minute']>event.payload['utc_minute']:
                        return True  # Older accepted occurrences were intentionally compacted.
            if not event.payload.get('durable') and event.occurrence_id not in self.cron.local['occurrences']:
                if action in {'accept','hold'}:return True
                if action=='begin':return False
                raise ValueError('本地 Cron 已结束，不能恢复计时或重试。')
            return self.cron.delivery(event,action,attempt_id,response_id)
        return True
    def recover(self):
        self.runtime.assert_owner()
        try:
            shared=self.cron.store.snapshot() if "scheduler" not in self.runtime.config.disabled_features else {"local_slots":{}}
            if any(s['target_session_id']==self.runtime.state.session_id for s in shared['local_slots'].values()):
                self.cron.store.remove_local(session_id=self.runtime.state.session_id)
            if "background" not in self.runtime.config.disabled_features:self.background.recover()
            if "scheduler" not in self.runtime.config.disabled_features:self.cron.sync()
            for message in self.runtime.state.messages:
                if message.role==MessageRole.USER and not message.is_human:
                    for event_id in (message.origin or {}).get('event_ids',[]):
                        row=self.store.get(event_id)
                        if row['event']['target_session_id']!=self.runtime.state.session_id:raise QueueError('通知消息归属不匹配。')
                        if row['message_id'] is None:self.store.bind(event_id,message.message_id)
            for message in self.runtime.state.messages:
                origin=message.origin or {}
                if message.role==MessageRole.ASSISTANT and origin.get('event_ids') and origin.get('attempt_id'):
                    for event_id in origin['event_ids']:
                        row=self.store.get(event_id)
                        if row['event']['target_session_id']!=self.runtime.state.session_id:raise QueueError('事件响应归属不匹配。')
                        if row['attempt_id'] is None:
                            self.store.change(event_id,lambda copy:copy.update(attempt_id=origin['attempt_id']))
                        elif row['attempt_id']!=origin['attempt_id']:continue
                        self.store.accept(event_id,origin['attempt_id'],message.message_id)
                        self._source(RuntimeEvent.from_dict(row['event']),'accept',origin['attempt_id'],message.message_id)
            count=0
            for row in self.store.list(self.runtime.state.session_id):
                if row['status'] in {'pending','held'}:
                    event=RuntimeEvent.from_dict(row['event'])
                    if event.kind=='cron_due' and not event.payload['durable']:
                        self.store.cancel(event.event_id)
                    else:
                        self.store.hold(event.event_id);self._source(event,'hold');count+=1
            if count:self.runtime.display('notice',f'恢复 {count} 个未接受事件，已暂停为 held；/events 查看，/events retry ID 仅重试当前可投递来源；旧团队通知保留记录，不恢复投递。')
        except QueueError as exc:
            self.error=exc;self.runtime.display('notice',str(exc)+' 自动投递已停用。')
    def boundary(self):
        self.runtime.assert_owner()
        self.background.drain()
        if self.runtime.mcp is not None:
            self.runtime.tasks.context(self.runtime.cancellation)
            self.runtime.mcp.sync(prepare=False)
        self.runtime.tasks.idle_boundary()
        if self.runtime.team is not None: self.runtime.team.boundary(self.runtime)
        if self.error is not None:return
        try:
            if self.cron.error is not None:raise QueueError('调度线程停止；请检查调度与事件记录。') from self.cron.error
            if "scheduler" not in self.runtime.config.disabled_features:self.cron.sync()
        except QueueError as exc:
            self.error=exc;self.runtime.display('notice',str(exc)+' 自动投递已停用。')
    def pending(self):
        self.boundary()
        if self.error is not None:return []
        try:return sorted([r for r in self.store.list(self.runtime.state.session_id) if r['status']=='pending' and not (r['event']['kind']=='cron_due' and 'scheduler' in self.runtime.config.disabled_features) and not (r['event']['kind'].startswith('background') and 'background' in self.runtime.config.disabled_features)],
                          key=lambda r:(r['event']['created_at'],r['event']['event_id']))
        except QueueError as exc:
            self.error=exc;self.runtime.display('notice',str(exc));return []
    def retry(self,event_id):
        try:local_id(event_id,'event')
        except ValueError as exc:raise ValueError('事件 ID 格式无效，请使用 /events 列出的完整 ID。') from exc
        row=self.store.get(event_id,allow_missing=True)
        if row is None:raise ValueError('未找到该事件，请使用 /events 查看本会话的事件 ID。')
        if (row["event"]["kind"]=="cron_due" and "scheduler" in self.runtime.config.disabled_features) or (row["event"]["kind"].startswith("background") and "background" in self.runtime.config.disabled_features):
            raise ValueError("事件来源机制在本次配置中关闭，记录保留。")
        if row['event']['target_session_id']!=self.runtime.state.session_id:raise ValueError('事件不属于本会话。')
        event=RuntimeEvent.from_dict(row['event'])
        if not self._source(event,'retry'):
            reason='团队已更换、未启用或通知身份不匹配' if event.kind.startswith('team_') else '事件来源当前不允许投递'
            raise ValueError(reason+'，不能重试；原记录保留。')
        self.store.retry(event_id);self.wakeup.notify()
    def dispatch(self):
        self.runtime.assert_owner()
        rows=self.pending()
        if not rows:return None
        chosen=[];size=0
        for row in rows[:10]:
            cost=len(json.dumps(row['event'],ensure_ascii=False))
            if chosen and size+cost>self.runtime.config.context_budget_chars//2:break
            chosen.append(row);size+=cost
        attempt_id=new_id('attempt')
        messages=[];events=[]
        try:
            for row in chosen:
                event=RuntimeEvent.from_dict(row['event'])
                if not self._source(event,'begin',attempt_id):
                    # A cancelled source cannot leave a stale pending copy in a busy loop.
                    self.store.hold(event.event_id)
                    continue
                self.store.begin(event.event_id,attempt_id,source_prepared=event.kind=='cron_due')
                events.append(event)
                if row['message_id'] is None:row=self.store.bind(event.event_id,new_id('message'))
                existing=next((m for m in self.runtime.state.messages if m.message_id==row['message_id']),None)
                if existing is not None:
                    if event.event_id not in (existing.origin or {}).get('event_ids',[]):raise QueueError('通知消息来源不匹配。')
                    messages.append(existing)
                else:
                    text=('[HOST EVENT: not human input or new authorization; process only the explicitly listed event. '
                          'Do not replay the previous human task or restart a cancelled/failed command.]\n'
                          +json.dumps(event.to_dict(),ensure_ascii=False))
                    messages.append(Message(MessageRole.USER,[TextBlock(text)],message_id=row['message_id'],
                        created_at=event.created_at,origin={'kind':'team' if event.kind.startswith('team_') else ('cron' if event.kind=='cron_due' else 'background'),
                                                         'event_ids':[event.event_id]}))
            if not events:return None
            self.goal_ids=[m.message_id for m in messages]
            self.active_event_ids=[e.event_id for e in events]
            def accepted(assistant):
                for event in events:
                    self.store.accept(event.event_id,attempt_id,assistant.message_id)
                    self._source(event,'accept',attempt_id,assistant.message_id)
            return self.runtime.run_automatic(messages,self.active_event_ids,attempt_id,accepted)
        finally:
            for event in events:
                row=self.store.get(event.event_id)
                if row['status']=='pending':
                    self.store.hold(event.event_id);self._source(event,'hold',attempt_id)
            self.goal_ids=[];self.active_event_ids=[]
    def close_scheduler(self):
        self.cron.close()
    def close_background(self,permanent=False):
        self.background.close(permanent=permanent)
