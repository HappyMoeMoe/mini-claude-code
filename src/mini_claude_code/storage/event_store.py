"""Small bounded file transactions for delivery and scheduler authorities."""

from contextlib import contextmanager
from copy import deepcopy
import fcntl
import json
import os
from pathlib import Path
import tempfile
import threading
import time

from ..core._validation import envelope, local_id, fields
from ..core.events import RuntimeEvent
from ..core.errors import StorageError
from .session_store import safe_path, durable_section

_GATE = threading.Lock()
_LOCKS = {}


class QueueError(StorageError):
    """Stop affected automatic dispatch; never replace a corrupt store."""


def read_json(workspace, path, *, max_bytes=16*1024*1024):
    try:
        safe_path(workspace, path)
        if path.stat().st_size > max_bytes: raise ValueError('file too large')
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError, TypeError, RecursionError, StorageError) as exc:
        raise QueueError('自动事件或调度记录不可读取；保留原文件并停止相关自动投递。') from exc


def atomic_json(workspace, path, data):
    name = None
    try:
        safe_path(workspace, path)
        payload = json.dumps(data, ensure_ascii=False, allow_nan=False).encode('utf-8')
        if len(payload) > 16*1024*1024: raise ValueError('file too large')
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, name = tempfile.mkstemp(prefix='.pending-',dir=path.parent)
        with os.fdopen(fd,'wb') as stream:
            stream.write(payload); stream.flush(); os.fsync(stream.fileno())
        with durable_section():
            os.replace(name,path)
            fd = os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
            try: os.fsync(fd)
            finally: os.close(fd)
    except (OSError, ValueError, StorageError) as exc:
        raise QueueError('自动事件、后台或调度记录写入失败；停止新的自动副作用。') from exc
    finally:
        if name is not None: Path(name).unlink(missing_ok=True)


@contextmanager
def file_lock(workspace, path):
    with _GATE: lock = _LOCKS.setdefault(str(path),threading.Lock())
    fd = None
    acquired = False
    deadline = time.monotonic()+1
    try:
        while not acquired:
            acquired = lock.acquire(timeout=0.02)
            if not acquired and time.monotonic()>=deadline: raise QueueError('自动记录正忙，请稍后重试。')
        safe_path(workspace,path)
        path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        fd = os.open(path,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        while True:
            try:
                fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB); break
            except BlockingIOError:
                if time.monotonic()>=deadline: raise QueueError('自动记录正忙，请稍后重试。')
                time.sleep(0.02)
        yield
    except OSError as exc:
        raise QueueError('无法锁定自动事件或调度记录。') from exc
    finally:
        if fd is not None: os.close(fd)
        if acquired: lock.release()


class EventStore:
    def __init__(self,workspace):
        self.workspace = Path(workspace)
        self.root = self.workspace/'.mini-claude-code/events'

    def path(self,event_id):
        local_id(event_id,'event')
        return safe_path(self.workspace,self.root/f'{event_id}.json')

    def _validate(self,data):
        envelope(data,{'event','status','message_id','attempt_id','response_id','cancel_requested'}, {'acknowledgement_kind'})
        event = RuntimeEvent.from_dict(data['event'])
        if event.kind not in {'cron_due','background_completed','background_failed','background_cancelled','team_message','team_status','team_plan','team_control'}:
            raise ValueError('unknown notification kind')
        payload=event.payload
        if event.kind.startswith('team_'):
            local_id(event.source_id,'team')
            fields(payload, {'team_id','sender','target','type','content','request_id'})
            if payload['team_id']!=event.source_id or payload['sender']!=event.source_agent_id: raise ValueError('team source mismatch')
            local_id(payload['sender'],'agent');local_id(payload['target'],'agent')
            if payload['request_id'] is not None: local_id(payload['request_id'],'request')
            if type(payload['content']) is not str or len(payload['content'])>8000: raise ValueError('message size')
            expected={'message':'team_message','status':'team_status','plan_request':'team_plan','plan_decision':'team_control','shutdown_request':'team_control','shutdown_response':'team_status'}
            if expected.get(payload['type'])!=event.kind:raise ValueError('message type')
            if payload['type'] not in {'message','status'} and payload['request_id'] is None:raise ValueError('missing protocol request')
        elif event.kind=='cron_due':
            local_id(event.source_id,'job');local_id(event.occurrence_id,'occurrence')
            fields(payload,{'job_id','prompt','timezone','utc_minute','durable','instruction'})
            if (payload['job_id']!=event.source_id or type(payload['prompt']) is not str or not payload['prompt'].strip()
                    or len(payload['prompt'])>8000 or type(payload['durable']) is not bool or type(payload['utc_minute']) is not int):
                raise ValueError('invalid Cron payload')
        elif not event.kind.startswith('team_'):
            local_id(event.source_id,'background')
            fields(payload,{'background_id','status','exit_code','artifact_id','preview','task_id','bytes_stored','truncated_at_capture','instruction'})
            local_id(payload['artifact_id'],'artifact')
            if payload['task_id'] is not None:local_id(payload['task_id'],'task')
            if (payload['background_id']!=event.source_id or event.kind!='background_'+payload['status']
                    or type(payload['preview']) is not str or len(payload['preview'])>2000
                    or type(payload['truncated_at_capture']) is not bool or type(payload['bytes_stored']) is not int):
                raise ValueError('invalid background payload')
        if data['status'] not in {'pending','accepted','held','cancelled'} or type(data['cancel_requested']) is not bool:
            raise ValueError('unknown delivery state')
        for field,kind in [('message_id','message'),('attempt_id','attempt'),('response_id','message')]:
            if data[field] is not None: local_id(data[field],kind)
        if data.get('acknowledgement_kind')=='host_applied' and event.kind!='team_control':raise ValueError('host acknowledgement on ordinary message')
        if data.get('acknowledgement_kind') not in {None,'host_applied','model_accepted'}: raise ValueError('ack kind')
        if data['status']=='accepted' and data.get('acknowledgement_kind')!='host_applied' and (data['response_id'] is None or data['attempt_id'] is None): raise ValueError('missing acceptance')
        if event.kind=='cron_due':
            local_id(event.source_id,'job');local_id(event.occurrence_id,'occurrence')
            if event.payload.get('job_id')!=event.source_id or type(event.payload.get('durable')) is not bool:raise ValueError('Cron source')
            if type(event.payload.get('prompt')) is not str or len(event.payload['prompt'])>8000:raise ValueError('Cron prompt')
        elif not event.kind.startswith('team_'):
            local_id(event.source_id,'background')
            if event.payload.get('background_id')!=event.source_id:raise ValueError('background source')
            local_id(event.payload.get('artifact_id'),'artifact')
            if type(event.payload.get('preview')) is not str or len(event.payload['preview'])>2000:raise ValueError('preview')
        return data

    def get(self,event_id,*,allow_missing=False):
        try:
            path=self.path(event_id)
            if allow_missing:
                # External lookup may miss; a record found here must still be
                # readable and valid, including if it disappears during reading.
                try:path.stat()
                except FileNotFoundError:return None
            data = self._validate(read_json(self.workspace,path))
            if data['event']['event_id']!=event_id: raise ValueError('event mismatch')
            return data
        except OSError as exc:raise QueueError('投递记录无法检查。') from exc
        except (ValueError,TypeError,KeyError) as exc: raise QueueError('投递记录结构无效。') from exc

    def list(self,session_id=None):
        if not safe_path(self.workspace,self.root).exists(): return []
        rows=[]
        for path in sorted(self.root.iterdir()):
            safe_path(self.workspace,path)
            if path.name=='store.lock' or path.name.startswith('.pending-'): continue
            if path.suffix!='.json' or not path.is_file(): raise QueueError('事件目录包含无效文件。')
            row=self.get(path.stem)
            if session_id is None or row['event']['target_session_id']==session_id: rows.append(row)
        return rows

    def put(self,event,*,held=False):
        with file_lock(self.workspace,self.root/'store.lock'):
            path=self.path(event.event_id)
            if path.exists():
                row=self.get(event.event_id)
                if row['event']!=event.to_dict(): raise QueueError('事件 ID 对应不同来源。')
                return row
            if event.kind=='team_message':
                outstanding=sum(r['event'].get('source_id')==event.source_id and r['event']['kind']=='team_message' and r['status'] in {'pending','held'} for r in self.list())
                if outstanding>=500: raise ValueError('团队未接受普通消息已达 500 条上限。')
            row={'schema_version':1,'event':event.to_dict(),'status':'held' if held else 'pending',
                 'message_id':None,'attempt_id':None,'response_id':None,'cancel_requested':False}
            try:self._validate(row)
            except (ValueError,KeyError,TypeError) as exc:raise QueueError('通知来源或正文结构无效。') from exc
            atomic_json(self.workspace,path,row)
            return row

    def change(self,event_id,operation):
        with file_lock(self.workspace,self.root/'store.lock'):
            row=self.get(event_id); updated=deepcopy(row)
            operation(updated)
            if updated!=row: atomic_json(self.workspace,self.path(event_id),self._validate(updated))
            return updated

    def bind(self,event_id,message_id):
        def update(row):
            if row['message_id'] is None: row['message_id']=message_id
        return self.change(event_id,update)

    def begin(self,event_id,attempt_id,*,source_prepared=False):
        def update(row):
            if (row['status']!='pending' and not (source_prepared and row['status']=='cancelled')
                    or row['cancel_requested'] and not source_prepared):
                raise ValueError('事件不再待投递。')
            row['status']='pending'
            row['attempt_id']=attempt_id
        return self.change(event_id,update)

    def accept(self,event_id,attempt_id,response_id):
        def update(row):
            if row['attempt_id']!=attempt_id: raise QueueError('事件接受来源与投递 attempt 不一致。')
            if row['status']=='accepted':return
            row['status']='accepted'; row['response_id']=response_id;row['acknowledgement_kind']='model_accepted'
        return self.change(event_id,update)

    def host_accept(self,event_id):
        def update(row):
            row['status']='accepted';row['acknowledgement_kind']='host_applied'
        return self.change(event_id,update)

    def hold(self,event_id):
        def update(row):
            if row['status']=='pending':
                row['status']='cancelled' if row['cancel_requested'] else 'held'
        return self.change(event_id,update)

    def retry(self,event_id):
        def update(row):
            if row['status']!='held' or row['cancel_requested']: raise ValueError('只有未撤销的 held 事件可重试。')
            row['status']='pending'; row['attempt_id']=None
        return self.change(event_id,update)

    def cancel(self,event_id,*,in_flight=False):
        def update(row):
            if row['status']=='accepted': return
            row['cancel_requested']=True
            if not in_flight: row['status']='cancelled'
            elif row['status']=='held': row['status']='cancelled'
        return self.change(event_id,update)
