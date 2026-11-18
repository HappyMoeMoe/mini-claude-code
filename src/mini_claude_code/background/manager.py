"""Bounded workers stage output; the owning main thread commits final authority."""

from dataclasses import dataclass,field
import json
from pathlib import Path
import queue
import threading
import time

from ..core._validation import new_id,local_id,utc_now,envelope,fields,timestamp
from ..core.cancellation import CancellationToken,Cancelled
from ..core.errors import StorageError
from ..core.events import RuntimeEvent
from ..storage.event_store import read_json,atomic_json,QueueError
from ..storage.session_store import safe_path, durable_section
from ..tools.shell import ShellRunner
from ..tools.registry import ToolOutput


@dataclass
class Work:
    record: dict
    context: object
    token: CancellationToken=field(default_factory=CancellationToken)
    ready: threading.Event=field(default_factory=threading.Event)
    thread: object=None
    started: bool=False
    error: object=None
    pid: int | None=None
    captured: int=0
    staged: int=0
    result: object=None
    uncertain: bool=False


class BackgroundManager:
    def __init__(self,runtime,events,wake):
        self.runtime,self.events,self.wake=runtime,events,wake
        self.root=runtime.config.data_dir/'background'
        self.work={}
        self.lock=threading.RLock()
        self.completed=queue.Queue(maxsize=4)
        self.stopped=False
        self.failed=False
    def path(self,background_id):
        local_id(background_id,'background')
        return safe_path(self.runtime.config.workspace,self.root/f'{background_id}.json')
    def _save(self,record):
        atomic_json(self.runtime.config.workspace,self.path(record['background_id']),record)
    def records(self):
        if not safe_path(self.runtime.config.workspace,self.root).exists():return []
        records=[]
        for path in self.root.iterdir():
            safe_path(self.runtime.config.workspace,path)
            if path.name=='staging' or path.name.startswith('.pending-'):continue
            local_id(path.stem,'background')
            row=read_json(self.runtime.config.workspace,path)
            try:
                envelope(row,{'background_id','owner_session_id','agent_id','turn_id','assistant_message_id','call_id','task_id','claim_id',
                    'cwd','command','timeout_seconds','process_group','started_at','ended_at','status','exit_code','artifact_id','event'})
                if row['background_id']!=path.stem:raise ValueError('id')
                for field,kind in [('owner_session_id','session'),('agent_id','agent'),('turn_id','turn'),('assistant_message_id','message')]:local_id(row[field],kind)
                if row['task_id'] is not None:local_id(row['task_id'],'task');local_id(row['claim_id'],'claim')
                elif row['claim_id'] is not None:raise ValueError('claim')
                from ..core.session import absolute_path
                absolute_path(row['cwd'],'background cwd')
                if type(row['command']) is not str or not row['command'] or type(row['call_id']) is not str or not row['call_id']:raise ValueError('command')
                if type(row['timeout_seconds']) is not int or not 1<=row['timeout_seconds']<=600:raise ValueError('timeout')
                timestamp(row['started_at'])
                if row['ended_at'] is not None:timestamp(row['ended_at'])
                if row['status'] not in {'starting','running','completed','failed','cancelled','execution_unknown'}:raise ValueError('status')
                if row['status'] in {'completed','failed','cancelled'} and row['ended_at'] is None:raise ValueError('missing end time')
                if row['status']=='completed' and row['exit_code']!=0:raise ValueError('completed exit code')
                if row['exit_code'] is not None and type(row['exit_code']) is not int:raise ValueError('exit')
                if row['process_group'] is not None:
                    fields(row['process_group'],{'pid','pgid'})
                    if any(type(v) is not int or v<1 for v in row['process_group'].values()):raise ValueError('process')
                if row['artifact_id'] is not None:local_id(row['artifact_id'],'artifact')
                if row['event'] is not None:
                    event=RuntimeEvent.from_dict(row['event'])
                    if event.source_id!=row['background_id'] or event.target_session_id!=row['owner_session_id']:raise ValueError('event owner')
            except (ValueError,TypeError,KeyError) as exc:raise QueueError('后台记录无效，不能信任其中的执行身份。') from exc
            if row['owner_session_id']==self.runtime.state.session_id:records.append(row)
        return records
    def usage(self):
        with self.lock:
            total=sum(entry.captured+entry.staged for entry in self.work.values())
            active=set(self.work)
        root=safe_path(self.runtime.config.workspace,self.root/'staging'/self.runtime.state.session_id)
        if root.exists():
            for path in root.iterdir():
                safe_path(self.runtime.config.workspace,path)
                if path.stem not in active:
                    if not path.is_file():raise StorageError('后台暂存包含非文件记录。')
                    total+=path.stat().st_size
        return total
    def has_running(self,task_id=None,*,unbound=False):
        with self.lock:
            return any(entry.thread is not None
                       and (entry.record['task_id'] is None if unbound else task_id is None or entry.record['task_id']==task_id)
                       for entry in self.work.values())
    def launch(self,args,context,invocation):
        if self.stopped or self.failed:raise StorageError('后台管理器已停止或存储状态不确定。')
        if self.runtime.automatic or self.runtime.child_spec is not None:raise PermissionError('自动轮和一次性子 Agent 不能启动后台工作。')
        with self.lock:
            if len(self.work)>=4:raise ValueError('最多 4 个运行中或待提交的后台命令，不自动排队。')
            call,message_id=invocation
            local_id(message_id,'message');local_id(self.runtime.loop.current_turn_id,'turn')
            assignment=self.runtime.tasks.assignment
            row={'schema_version':1,'background_id':new_id('background'),'owner_session_id':context.session_id,
                 'agent_id':context.agent_id,'turn_id':self.runtime.loop.current_turn_id,'assistant_message_id':message_id,
                 'call_id':call.call_id,'task_id':context.task_id,'claim_id':assignment.claim_id if assignment else None,
                 'cwd':context.cwd,'command':args['command'],'timeout_seconds':args['timeout_seconds'],
                 'process_group':None,'started_at':utc_now(),'ended_at':None,'status':'starting','exit_code':None,
                 'artifact_id':None,'event':None}
            self._save(row)
            entry=Work(row,context)
            self.work[row['background_id']]=entry
        entry.thread=threading.Thread(target=self._worker,args=(entry,args),name='mini-cc-background',daemon=True)
        try:entry.thread.start()
        except Exception as exc:
            if entry.thread.is_alive():
                entry.token.cancel();raise StorageError('后台线程启动状态不确定。') from exc
            row.update(status='failed',ended_at=utc_now());self._save(row)
            with self.lock:self.work.pop(row['background_id'],None)
            raise ValueError('后台线程未能启动。') from exc
        deadline=time.monotonic()+3
        while not entry.ready.wait(0.02):
            if self.runtime.cancellation is not None:self.runtime.cancellation.check()
            if time.monotonic()>deadline:
                entry.token.cancel()
                raise StorageError('后台启动状态无法确认；请检查后台记录。')
        if not entry.started:
            entry.thread.join(timeout=1)
            self.drain()
            raise ValueError('后台进程未能启动。')
        row['status']='running';row['process_group']={'pid':entry.pid,'pgid':entry.pid}
        self._save(row)
        return ToolOutput(f'后台命令已启动：{row["background_id"]}；最终退出状态将由独立通知报告。',
                          metadata={'background_id':row['background_id'],'launch':True,'state':'running'})
    def _stage_path(self,entry):
        return safe_path(self.runtime.config.workspace,self.root/'staging'/self.runtime.state.session_id/f'{entry.record["background_id"]}.txt')
    def _worker(self,entry,args):
        from ..storage.artifacts import SINGLE_LIMIT,SESSION_LIMIT
        try:runner=ShellRunner()
        except BaseException as exc:
            entry.error=exc;entry.ready.set();self.completed.put_nowait(entry.record['background_id']);self.wake();return
        runner.cancellation=entry.token
        def started(process):
            entry.pid=process.pid;entry.started=True;entry.ready.set()
        def capture(count):
            with self.runtime.artifacts.quota_lock:
                if self.runtime.artifacts.usage()+self.runtime.artifacts.foreground_capture+self.usage()-entry.captured+count>SESSION_LIMIT:
                    raise StorageError('后台捕获超过会话配额。')
                with self.lock:entry.captured=count
        def sink(output,context,invocation,captured):
            raw=output.output.encode('utf-8')
            metadata=dict(output.metadata or {})
            if len(raw)>SINGLE_LIMIT:
                raw=raw[:SINGLE_LIMIT].decode('utf-8',errors='ignore').encode('utf-8')
                metadata['truncated_at_capture']=True;metadata['bytes_total']=None
            with self.runtime.artifacts.quota_lock:
                if self.runtime.artifacts.usage()+self.runtime.artifacts.foreground_capture+self.usage()+len(raw)>SESSION_LIMIT:
                    raise StorageError('后台暂存超过会话配额。')
                with self.lock:entry.staged=len(raw)
                path=self._stage_path(entry)
                path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
                with path.open('xb') as stream:stream.write(raw);stream.flush()
            return ToolOutput('',output.status,metadata)
        runner.on_started=started;runner.capture_check=capture;runner.output_sink=sink
        try:
            entry.token.check()
            entry.result=runner.run({**args,'run_in_background':False},entry.context)
        except BaseException as exc:entry.error=exc
        finally:
            try:runner.close()
            except BaseException as exc:entry.error=exc
            entry.uncertain=runner.unconfirmed_work or bool(runner._active)
            with self.runtime.artifacts.quota_lock:
                with self.lock:entry.captured=0
            entry.ready.set()
            try:self.completed.put_nowait(entry.record['background_id'])
            except queue.Full:entry.error=StorageError('后台结果队列已满。')
            self.wake()
    def drain(self):
        if self.failed:raise StorageError('后台输出提交失败，需检查记录和产物后恢复。')
        try:
            with durable_section():self._drain()
        except Exception as exc:
            self.failed=True
            if isinstance(exc,StorageError):raise
            raise StorageError('后台结果提交失败；停止新的副作用。') from exc

    def _drain(self):
        while True:
            try:background_id=self.completed.get_nowait()
            except queue.Empty:break
            with self.lock:entry=self.work[background_id]
            entry.thread.join(timeout=1)
            if entry.thread.is_alive() or entry.uncertain:
                entry.record.update(status='execution_unknown',ended_at=utc_now())
                self._save(entry.record)
                self.runtime.cleanup_uncertain=True
                raise StorageError('后台资源未确认停止；保留任务认领。')
            row=entry.record
            if entry.pid is not None:row['process_group']={'pid':entry.pid,'pgid':entry.pid}
            if entry.error is not None:
                row['status']='execution_unknown' if entry.started else 'cancelled' if isinstance(entry.error,Cancelled) else 'failed'
                row['ended_at']=utc_now()
                self._save(row)
                if entry.started:raise StorageError('后台输出未可靠捕获或保存；停止新的副作用。') from entry.error
            else:
                path=self._stage_path(entry)
                with path.open('r',encoding='utf-8',newline='') as stream:text=stream.read()
                metadata=entry.result.metadata or {}
                record=self.runtime.artifacts.save(text,call_id=row['call_id'],assistant_message_id=row['assistant_message_id'],
                    tool_name='bash',metadata={**metadata,**({'task_id':row['task_id']} if row['task_id'] else {})})
                status='cancelled' if metadata.get('cancelled') else 'completed' if entry.result.status.value=='success' else 'failed'
                preview=text if len(text)<=1800 else self.runtime.artifacts.preview(text,record)
                event=RuntimeEvent('background_'+('completed' if status=='completed' else 'cancelled' if status=='cancelled' else 'failed'),
                    row['owner_session_id'],{'background_id':background_id,'status':status,'exit_code':metadata.get('exit_code'),
                    'artifact_id':record['artifact_id'],'preview':preview,'task_id':row['task_id'],
                    'bytes_stored':record['bytes_stored'],'truncated_at_capture':record['truncated_at_capture'],
                    'instruction':'仅报告真实结果；不要因通知自动重跑命令。'},
                    source_agent_id=row['agent_id'],source_id=background_id)
                row.update(status=status,ended_at=utc_now(),exit_code=metadata.get('exit_code'),
                           artifact_id=record['artifact_id'],event=event.to_dict())
                self._save(row)
                self.events.put(event)
                with self.runtime.artifacts.quota_lock:
                    path.unlink()
                    with self.lock:entry.staged=0
            with self.lock:self.work.pop(background_id,None)
    def cancel(self,background_id):
        local_id(background_id,'background')
        with self.lock:entry=self.work.get(background_id)
        if entry is None:
            record=next((r for r in self.records() if r['background_id']==background_id),None)
            if record is None:raise ValueError('本 Runtime 没有该后台执行。')
            return record['status']
        entry.token.cancel()
        return 'cancellation_requested'
    def close(self,permanent=False):
        self.stopped=self.stopped or permanent
        with self.lock:entries=list(self.work.values())
        for entry in entries:entry.token.cancel()
        deadline=time.monotonic()+4
        for entry in entries:
            if entry.thread is not None:entry.thread.join(timeout=max(0,deadline-time.monotonic()))
        if any(e.thread is not None and e.thread.is_alive() for e in entries):
            self.runtime.cleanup_uncertain=True
            raise StorageError('后台线程或进程未确认停止。')
        self.drain()
    def recover(self):
        unknown=0
        for row in self.records():
            if row['status'] in {'starting','running'}:
                row.update(status='execution_unknown',ended_at=utc_now());self._save(row)
            if row['status']=='execution_unknown':unknown+=1
            if row['event'] is not None:self.events.put(RuntimeEvent.from_dict(row['event']),held=True)
        if unknown:self.runtime.display('notice',f'{unknown} 条后台执行状态不明；不会重跑或向旧 PID 发信号，请用 /background 检查命令与 cwd。')
