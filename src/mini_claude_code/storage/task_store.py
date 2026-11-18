"""One task per file, graph-wide bounded locks and atomic single-node updates."""

from collections import deque
from contextlib import contextmanager, nullcontext
from dataclasses import replace
import fcntl
import json
import os
from pathlib import Path
import tempfile
import threading
import time

from ..core._validation import local_id, new_id, utc_now
from ..core.cancellation import CancellationToken
from ..core.errors import StorageError
from ..tasks.models import TaskRecord, TaskError, bounded, validate_source
from .session_store import safe_path, durable_section

_LOCKS = {}
_LOCKS_GATE = threading.Lock()
MAX_TASKS = 1000
MAX_RECORD_BYTES = 128 * 1024
LOCK_TIMEOUT = 1.0


class TaskDataError(StorageError):
    """Invalid authority: stop the execution chain and inspect storage."""


class TaskWriteError(StorageError):
    """Task persistence is uncertain; never convert this to an ordinary tool error."""


def validate_graph(graph):
    incoming = {task_id:len(record.blocked_by) for task_id,record in graph.items()}
    downstream = {task_id:[] for task_id in graph}
    owners = set()
    for task_id, record in graph.items():
        for dependency in record.blocked_by:
            if dependency not in graph:
                raise TaskError('任务引用不存在的依赖。')
            downstream[dependency].append(task_id)
        if record.owner is not None:
            owner = record.owner['agent_id']
            if owner in owners:
                raise TaskError('同一执行者不能持有两个认领。')
            owners.add(owner)
            if any(graph[dep].status != 'completed' for dep in record.blocked_by):
                raise TaskError('进行中的任务仍有未完成依赖。')
        if record.status == 'completed' and any(graph[dep].status != 'completed' for dep in record.blocked_by):
            raise TaskError('已完成任务存在未完成依赖。')
    queue = deque(task_id for task_id,count in incoming.items() if count == 0)
    visited = 0
    while queue:
        task_id = queue.popleft()
        visited += 1
        for child in downstream[task_id]:
            incoming[child] -= 1
            if incoming[child] == 0:
                queue.append(child)
    if visited != len(graph):
        raise TaskError('任务依赖不能形成环。')


def view(record, graph, *, brief=False):
    incomplete = [dep for dep in record.blocked_by if graph[dep].status != 'completed']
    result = record.to_dict()
    if brief:
        result = {key:result[key] for key in ('task_id','subject','status','owner','revision','blocked_by')}
    return {**result, 'ready':record.status == 'pending' and not incomplete and (record.worktree is None or record.worktree['status']=='ready'),
            'blocked':record.status == 'pending' and bool(incomplete), 'incomplete_dependencies':incomplete}


def authorize_completion(record, assignment, source, *, team_id=None):
    """Authorize both a state transition and an idempotent result disclosure."""
    validate_source(source)
    if team_id is not None:
        local_id(team_id, 'team')
        if record.team_id != team_id:
            raise TaskError('任务不属于本团队。')
    if record.status == 'completed':
        owner = record.completed_by['owner']
        if (owner['agent_id'] != source['agent_id'] or owner['session_id'] != source['session_id']
                or (assignment is not None and (assignment.task_id != record.task_id or assignment.owner != owner))):
            raise TaskError('只有原完成者可重复确认已完成任务；当前 assignment 必须匹配原 claim。')
    elif (record.status != 'in_progress' or assignment is None or assignment.task_id != record.task_id
          or record.owner != assignment.owner or record.owner['agent_id'] != source['agent_id']
          or record.owner['session_id'] != source['session_id']):
        raise TaskError('只有持有当前 claim 的真实 owner 才能完成任务。')


class TaskStore:
    def __init__(self, workspace):
        self.workspace = Path(workspace).resolve(strict=True)
        self.root = self.workspace / '.mini-claude-code' / 'tasks'
        self.failed = False
        with _LOCKS_GATE:
            self._thread_lock = _LOCKS.setdefault(str(self.root), threading.Lock())

    def _path(self, name):
        return safe_path(self.workspace, self.root / name)

    @contextmanager
    def _locked(self, cancellation=None):
        token = cancellation or CancellationToken()
        fd = None
        acquired = False
        deadline = time.monotonic() + LOCK_TIMEOUT
        try:
            if self.failed:
                raise TaskWriteError('任务存储写入状态不确定，请停止并检查。')
            while not acquired:
                token.check()
                acquired = self._thread_lock.acquire(timeout=0.02)
                if not acquired and time.monotonic() >= deadline:
                    raise TaskError('任务库正忙，可稍后重试。')
            self._path('store.lock')
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(self._path('store.lock'), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            while True:
                token.check()
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TaskError('任务库正忙，可稍后重试。')
                    token.wait(0.02)
            token.check()
            yield
        except OSError as exc:
            raise TaskDataError('无法安全访问或锁定任务库。') from exc
        finally:
            if fd is not None:
                os.close(fd)
            if acquired:
                self._thread_lock.release()

    def _read_all(self):
        try:
            graph = {}
            for path in sorted(self.root.iterdir()):
                self._path(path.name)
                if not path.is_file():
                    raise TaskError('任务目录包含非普通文件。')
                if path.name == 'store.lock' or path.name.startswith('.pending-'):
                    continue
                if path.suffix != '.json':
                    raise TaskError('任务目录包含无法识别的文件。')
                local_id(path.stem, 'task')
                if len(graph) >= MAX_TASKS or path.stat().st_size > MAX_RECORD_BYTES:
                    raise TaskError('任务库超过节点或文件大小限制。')
                record = TaskRecord.from_dict(json.loads(path.read_text(encoding='utf-8')))
                if record.task_id != path.stem:
                    raise TaskError('任务 ID 与文件名不匹配。')
                graph[record.task_id] = record
            validate_graph(graph)
            return graph
        except (OSError, ValueError, TypeError, KeyError, RecursionError, StorageError) as exc:
            raise TaskDataError('任务库损坏、版本不支持或路径无效；不会覆盖或忽略异常节点。') from exc

    def snapshot(self, cancellation=None):
        if not self._path('store.lock').parent.exists():
            return {}
        with self._locked(cancellation):
            return self._read_all()

    def _write(self, record, cancellation=None):
        payload = json.dumps(record.to_dict(), ensure_ascii=False, allow_nan=False).encode('utf-8')
        if len(payload) > MAX_RECORD_BYTES:
            raise TaskError('任务记录超过保存上限。')
        if cancellation is not None:
            cancellation.check()
        name = None
        try:
            fd, name = tempfile.mkstemp(prefix='.pending-', dir=self.root)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            if cancellation is not None:
                cancellation.check()
            with durable_section():
                os.replace(name, self._path(f'{record.task_id}.json'))
                fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
        except (OSError, StorageError) as exc:
            self.failed = True
            raise TaskWriteError('任务写入无法确认；停止后续操作并检查任务及会话来源。') from exc
        finally:
            if name is not None:
                Path(name).unlink(missing_ok=True)

    def _get(self, graph, task_id):
        local_id(task_id, 'task')
        if task_id not in graph:
            raise TaskError('指定任务不存在。')
        return graph[task_id]

    def create(self, subject, description, source, cancellation=None, *, team_id=None):
        bounded(subject, 'subject', 200)
        bounded(description, 'description', 8000, empty=True)
        validate_source(source)
        with self._locked(cancellation):
            graph = self._read_all()
            if len(graph) >= MAX_TASKS:
                raise TaskError('任务已达 1000 个节点上限。')
            now = utc_now()
            record = TaskRecord(new_id('task'), subject.strip(), description, 'pending', [], None,
                                1, now, now, source, source, team_id=team_id)
            if record.task_id in graph:
                raise TaskError('宿主生成的任务 ID 冲突；不会覆盖已有任务。')
            self._write(record, cancellation)
            return record

    def add_dependencies(self, task_id, dependencies, source, cancellation=None):
        if type(dependencies) is not list or not dependencies:
            raise TaskError('add_blocked_by 必须是非空数组。')
        validate_source(source)
        with self._locked(cancellation):
            graph = self._read_all()
            record = self._get(graph, task_id)
            if record.status != 'pending' or record.owner is not None:
                raise TaskError('仅可为尚未开始的任务添加依赖。')
            for dependency in dependencies:
                self._get(graph, dependency)
            combined = list(dict.fromkeys(record.blocked_by + dependencies))
            if combined == record.blocked_by:
                return record
            updated = replace(record, blocked_by=combined, revision=record.revision+1,
                              updated_at=utc_now(), last_modified_by=source)
            graph[task_id] = updated
            validate_graph(graph)
            self._write(updated, cancellation)
            return updated

    def claim(self, task_id, source, cancellation=None, *, team_id=None, adopt_session=None, adopt_scope=None, prepare=None, automatic=False):
        validate_source(source)
        with self._locked(cancellation):
            graph = self._read_all()
            record = self._get(graph, task_id)
            # Recovery holds old session locks outside this graph lock, then
            # validates/holds the old team authority through the atomic claim.
            with adopt_scope(record, graph) if adopt_scope is not None else nullcontext():
                if team_id is not None and record.team_id != team_id:
                    if not (not automatic and adopt_session and record.created_by['session_id']==adopt_session
                            and record.status=='pending' and record.owner is None
                            and (record.team_id is None or adopt_scope is not None)):
                        raise TaskError('任务不属于本团队。')
                    record = replace(record, team_id=team_id)
                if automatic and record.auto_claim_blocked: raise TaskError('失败任务须由 lead 显式重新派发。')
                if not view(record, graph)['ready'] or record.owner is not None:
                    raise TaskError('任务不是可认领的 ready 状态。')
                if any(r.owner and r.owner['agent_id'] == source['agent_id'] for r in graph.values()):
                    raise TaskError('当前执行者已有进行中的任务，不能重复认领。')
                owner = {'agent_id':source['agent_id'],'session_id':source['session_id'],'claim_id':new_id('claim')}
                updated = replace(record, status='in_progress', owner=owner, claim_source=source, auto_claim_blocked=False,
                                  revision=record.revision+1, updated_at=utc_now(), last_modified_by=source)
                if prepare is not None: prepare(updated)
                self._write(updated, cancellation)
                return updated

    def complete(self, task_id, assignment, summary, source, cancellation=None, *, team_id=None):
        bounded(summary, 'summary', 2000, empty=True)
        validate_source(source)
        with self._locked(cancellation):
            graph = self._read_all()
            record = self._get(graph, task_id)
            authorize_completion(record, assignment, source, team_id=team_id)
            if record.status == 'completed':
                return record, [], False
            updated = replace(record, status='completed', owner=None, completion=summary,
                completed_by={'owner':assignment.owner,'source':source}, last_modified_by=source,
                revision=record.revision+1, updated_at=utc_now())
            graph[task_id] = updated
            ready = [r.task_id for r in graph.values() if task_id in r.blocked_by and view(r, graph)['ready']]
            self._write(updated, cancellation)
            return updated, ready, True

    def release(self, task_id, owner, revision, source, reason, cancellation=None, *, auto_claim_blocked=False):
        validate_source(source)
        bounded(reason, 'reason', 500)
        with self._locked(cancellation):
            graph = self._read_all()
            record = self._get(graph, task_id)
            if record.status != 'in_progress' or record.owner != owner or record.revision != revision:
                raise TaskError('认领或任务版本已变化，本次释放未提交。')
            now = utc_now()
            updated = replace(record, status='pending', owner=None, auto_claim_blocked=auto_claim_blocked, revision=record.revision+1,
                updated_at=now, last_modified_by=source,
                last_release={'owner':owner,'source':source,'reason':reason,'released_at':now})
            self._write(updated, cancellation)
            return updated
