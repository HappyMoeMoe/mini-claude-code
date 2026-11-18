"""Host-owned persistent task records and process-local assignments."""

from dataclasses import dataclass, asdict

from ..core._validation import envelope, fields, integer, local_id, timestamp
from ..core.session import absolute_path


class TaskError(ValueError):
    """A business conflict or bounded lock contention; no uncertain write."""


def bounded(value, name, limit, *, empty=False):
    if type(value) is not str or len(value) > limit or (not empty and not value.strip()):
        raise TaskError(f'{name} 长度无效（上限 {limit} 字符）。')


def validate_source(source):
    fields(source, {'agent_id','session_id','assistant_message_id','call_id','kind'})
    local_id(source['agent_id'], 'agent')
    local_id(source['session_id'], 'session')
    if source['kind'] not in {'tool','host','manual'}:
        raise TaskError('任务操作来源无效。')
    if source['kind'] == 'tool':
        local_id(source['assistant_message_id'], 'message')
        bounded(source['call_id'], 'call_id', 1000)
    elif source['assistant_message_id'] is not None or source['call_id'] is not None:
        raise TaskError('宿主操作不能冒充模型工具调用。')


def validate_owner(owner):
    fields(owner, {'agent_id','session_id','claim_id'})
    for field, kind in [('agent_id','agent'),('session_id','session'),('claim_id','claim')]:
        local_id(owner[field], kind)


@dataclass(frozen=True)
class TaskRecord:
    task_id: str
    subject: str
    description: str
    status: str
    blocked_by: list[str]
    owner: dict | None
    revision: int
    created_at: str
    updated_at: str
    created_by: dict
    last_modified_by: dict
    claim_source: dict | None = None
    completed_by: dict | None = None
    completion: str | None = None
    last_release: dict | None = None
    team_id: str | None = None
    auto_claim_blocked: bool = False
    worktree: dict | None = None

    def __post_init__(self):
        local_id(self.task_id, 'task')
        if self.team_id is not None: local_id(self.team_id, 'team')
        if type(self.auto_claim_blocked) is not bool: raise TaskError('auto_claim_blocked 必须为布尔值。')
        if self.worktree is not None:
            fields(self.worktree, {'operation_id','status','name','path','branch','base_commit'})
            local_id(self.worktree['operation_id'], 'operation')
            if self.worktree['status'] not in {'preparing','ready','partial','failed','removed'}: raise TaskError('Worktree 状态无效。')
            for key in ('name','path','branch','base_commit'):
                if type(self.worktree[key]) is not str: raise TaskError('Worktree 字段无效。')
        bounded(self.subject, 'subject', 200)
        bounded(self.description, 'description', 8000, empty=True)
        if self.status not in {'pending','in_progress','completed'}:
            raise TaskError('任务状态无效。')
        if type(self.blocked_by) is not list or len(self.blocked_by) > 50:
            raise TaskError('每项任务最多 50 个依赖。')
        for task_id in self.blocked_by:
            local_id(task_id, 'task')
        if len(set(self.blocked_by)) != len(self.blocked_by) or self.task_id in self.blocked_by:
            raise TaskError('任务依赖重复或指向自身。')
        integer(self.revision, 'revision', minimum=1)
        timestamp(self.created_at); timestamp(self.updated_at)
        validate_source(self.created_by); validate_source(self.last_modified_by)
        if self.claim_source is not None:
            validate_source(self.claim_source)
        if self.owner is not None:
            validate_owner(self.owner)
        if (self.status == 'in_progress') != (self.owner is not None):
            raise TaskError('任务状态与负责人不匹配。')
        if self.status == 'in_progress' and self.claim_source is None:
            raise TaskError('认领缺少来源。')
        if self.status == 'completed':
            fields(self.completed_by, {'owner','source'})
            validate_owner(self.completed_by['owner'])
            validate_source(self.completed_by['source'])
            bounded(self.completion, 'completion', 2000, empty=True)
            if self.claim_source is None:
                raise TaskError('完成记录缺少认领来源。')
        elif self.completed_by is not None or self.completion is not None:
            raise TaskError('未完成任务不能包含完成简报。')
        if self.last_release is not None:
            fields(self.last_release, {'owner','source','reason','released_at'})
            validate_owner(self.last_release['owner'])
            validate_source(self.last_release['source'])
            bounded(self.last_release['reason'], 'release reason', 500)
            timestamp(self.last_release['released_at'])

    def to_dict(self):
        self.__post_init__()
        return {'schema_version':1, **asdict(self)}

    @classmethod
    def from_dict(cls, data):
        optional = {'team_id','auto_claim_blocked','worktree'}
        envelope(data, set(cls.__dataclass_fields__)-optional, optional)
        return cls(**{key:value for key,value in data.items() if key != 'schema_version'})


@dataclass(frozen=True)
class Assignment:
    task_id: str
    claim_id: str
    agent_id: str
    session_id: str
    cwd: str
    binding: dict | None = None

    def __post_init__(self):
        local_id(self.task_id, 'task')
        validate_owner(self.owner)
        absolute_path(self.cwd, 'assignment cwd')

    @property
    def owner(self):
        return {'agent_id':self.agent_id,'session_id':self.session_id,'claim_id':self.claim_id}

    @classmethod
    def from_record(cls, record, cwd):
        if record.status != 'in_progress':
            raise TaskError('无法从未认领任务建立 assignment。')
        return cls(record.task_id, cwd=cwd, binding=dict(record.worktree) if record.worktree else None, **record.owner)
