"""Team and protocol authorities; persisted decisions precede notification."""
from contextlib import contextmanager
from pathlib import Path

from ..core._validation import local_id, utc_now, envelope
from ..core.errors import StorageError
from ..storage.event_store import atomic_json, read_json, file_lock
from ..storage.session_store import safe_path

STATES={'STARTING','IDLE','WORKING','WAITING_APPROVAL','STOPPING','STOPPED','FAILED','interrupted'}


class TeamStore:
    def __init__(self, workspace, team_id):
        local_id(team_id,'team')
        self.workspace=Path(workspace);self.team_id=team_id
        self.root=self.workspace/'.mini-claude-code/teams'/team_id

    def path(self,name):return safe_path(self.workspace,self.root/name)

    def read(self):
        data=read_json(self.workspace,self.path('team.json'))
        envelope(data,{'team_id','lead_agent_id','lead_session_id','members','revision','created_at'})
        if data['team_id']!=self.team_id or type(data['members']) is not dict:raise StorageError('团队记录损坏。')
        local_id(data['lead_agent_id'],'agent');local_id(data['lead_session_id'],'session')
        names=set()
        for agent_id,m in data['members'].items():
            local_id(agent_id,'agent');local_id(m['session_id'],'session')
            if m['agent_id']!=agent_id or m['state'] not in STATES or m['name'].lower() in names:raise StorageError('团队成员记录损坏。')
            names.add(m['name'].lower())
            if m['plan_status'] not in {'not_required','required','pending','approved','rejected'}:raise StorageError('计划状态损坏。')
        return data

    @contextmanager
    def transaction(self):
        with file_lock(self.workspace,self.path('state.lock')):
            data=self.read()
            yield data
            data['revision']+=1
            atomic_json(self.workspace,self.path('team.json'),data)

    def create(self, lead):
        data={'schema_version':1,'team_id':self.team_id,'lead_agent_id':lead.agent_id,'lead_session_id':lead.session_id,
              'members':{},'revision':1,'created_at':utc_now()}
        atomic_json(self.workspace,self.path('team.json'),data)

    def request(self,request_id,*,allow_missing=False):
        local_id(request_id,'request')
        path=self.path('requests/'+request_id+'.json')
        if allow_missing:
            # Only a caller-supplied, unreferenced ID may be absent. Once a
            # record exists, read failures (including deletion races) are fatal.
            try:path.stat()
            except FileNotFoundError:return None
            except OSError as exc:raise StorageError('团队协议记录无法检查。') from exc
        row=read_json(self.workspace,path)
        try:
            if type(row) is not dict or row.get('schema_version')!=1 or row.get('request_id')!=request_id or row.get('team_id')!=self.team_id:
                raise ValueError('request identity')
            if row.get('type') not in {'plan','shutdown'} or row.get('status') not in {'pending','approved','rejected','applied','stopped','stale'}:
                raise ValueError('request state')
            for key in ('sender','reviewer'):local_id(row[key],'agent')
        except (ValueError,TypeError,KeyError) as exc:
            raise StorageError('团队协议记录损坏。') from exc
        return row

    def save_request(self,row):
        atomic_json(self.workspace,self.path('requests/'+row['request_id']+'.json'),row)

    def requests(self):
        directory=self.path('requests')
        return [self.request(p.stem) for p in sorted(directory.glob('*.json'))] if directory.exists() else []

    @classmethod
    def interrupt_old(cls,workspace,session_id):
        root=Path(workspace)/'.mini-claude-code/teams'
        if not safe_path(Path(workspace),root).exists():return 0
        interrupted=0
        for p in sorted(root.iterdir()):
            store=cls(workspace,p.name);data=store.read()
            if data['lead_session_id']!=session_id:continue
            with store.transaction() as data:
                for member in data['members'].values():
                    if member['state'] not in {'STOPPED','FAILED','interrupted'}:
                        interrupted+=1
                        member.update(state='interrupted',plan_status='required' if member['require_plan'] else 'not_required',latest_plan=None)
                for request in store.requests():
                    if request['status'] in {'pending','approved'}:
                        request['status']='stale';store.save_request(request)
        return interrupted
