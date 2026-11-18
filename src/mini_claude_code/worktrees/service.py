"""Git operations outside task locks; partial outcomes are preserved for inspection."""

from dataclasses import replace
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

from ..core._validation import new_id, utc_now, local_id
from ..core.errors import StorageError
from ..core.messages import ToolSpec
from ..storage.event_store import atomic_json, read_json, file_lock
from ..storage.session_store import safe_path
from ..storage.task_store import TaskStore
from ..tasks.models import TaskError
from ..tools.registry import ToolOutput


# Invocation-only overrides: never rewrite the user's repository/global config.
# An empty fsmonitor value also disables hook-based monitors on older Git versions
# that interpret the string "false" as an executable path.
HOST_GIT_OPTIONS = (
    '-c', f'core.hooksPath={os.devnull}',
    '-c', 'core.fsmonitor=',
    '-c', 'maintenance.auto=false',
    '-c', 'gc.auto=0',
)
EXTERNAL_FILTERS = r'^filter\..*\.(clean|smudge|process)$'


class GitVerificationUnavailable(TaskError):
    """Git supplied no reliable observation; this does not prove a broken binding."""


def safe_name(value):
    if type(value) is not str or re.fullmatch(r'[A-Za-z0-9_-]{1,64}', value) is None:
        raise TaskError('名称必须为 1–64 位字母、数字、下划线或连字符。')
    return value


class DirectoryLease:
    """An OS-released advisory lock, owned by an actual executor, never by a model name."""
    def __init__(self, workspace, root, owner):
        self.workspace, self.root, self.owner = Path(workspace), Path(root), owner
        self.fd = None
        self.path = None
        key = hashlib.sha256(str(self.root).encode()).hexdigest()
        path = safe_path(self.workspace, self.workspace/'.mini-claude-code/leases'/f'{key}.lock')
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path=path
        fd = os.open(path, os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX|fcntl.LOCK_NB)
            payload = json.dumps({'root':str(root),'owner':owner},ensure_ascii=False).encode()
            os.ftruncate(fd,0);os.write(fd,payload);os.fsync(fd)
            self.fd = fd
        except BlockingIOError as exc:
            os.close(fd)
            raise TaskError('执行目录正被另一执行者租用，请等待或选择独立 Worktree。') from exc
        except BaseException:
            os.close(fd);raise

    def verify(self):
        if self.fd is None:raise StorageError('执行目录租用已失效。')
        try:
            safe_path(self.workspace,self.path)
            actual=self.path.stat();held=os.fstat(self.fd)
            if (actual.st_dev,actual.st_ino)!=(held.st_dev,held.st_ino):raise StorageError('执行目录锁文件已替换。')
        except OSError as exc:raise StorageError('执行目录租用无法验证。') from exc

    def assign(self,owner):
        self.verify();self.owner=owner
        payload=json.dumps({'root':str(self.root),'owner':owner},ensure_ascii=False).encode()
        try:
            os.lseek(self.fd,0,os.SEEK_SET);os.ftruncate(self.fd,0);os.write(self.fd,payload);os.fsync(self.fd)
        except OSError as exc:raise StorageError('目录租用身份保存失败。') from exc

    def close(self):
        if self.fd is not None: os.close(self.fd);self.fd=None


class WorktreeService:
    def __init__(self, workspace, tasks=None):
        self.workspace = Path(workspace).resolve(strict=True)
        self.tasks = tasks or TaskStore(self.workspace)
        self.root = self.workspace/'.mini-claude-code/worktrees'

    def _path(self, name):
        return safe_path(self.workspace,self.root/name)

    def git(self, *args, cwd=None):
        command = ['git', *HOST_GIT_OPTIONS]
        options = dict(cwd=cwd or self.workspace, text=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20,
                       env={**os.environ, 'GIT_TERMINAL_PROMPT': '0', 'GIT_OPTIONAL_LOCKS': '0'})
        try:
            # Checkout and status can invoke clean/smudge/process programs even
            # with hooks disabled. Do not silently bypass filters (e.g. LFS) and
            # materialize different files; require explicit Shell approval instead.
            filters = subprocess.run(
                [*command, 'config', '--null', '--get-regexp', EXTERNAL_FILTERS], **options)
            if filters.returncode not in {0, 1}:
                raise GitVerificationUnavailable('无法校验 Git 外部过滤器配置，未执行自动 Git 操作。')
            for entry in filters.stdout.split('\0'):
                if not entry:
                    continue
                _, separator, value = entry.partition('\n')
                if not separator:
                    raise GitVerificationUnavailable('Git 外部过滤器配置无法解析，未执行自动 Git 操作。')
                if value.strip():
                    raise GitVerificationUnavailable('仓库配置了外部 Git 过滤器，自动 Worktree/Git 操作已拒绝；'
                                    '需要运行过滤器时，请通过前台逐次审批的 Shell 操作。')
            proc = subprocess.run([*command, *args], **options)
        except (OSError,subprocess.TimeoutExpired,UnicodeError) as exc:
            raise GitVerificationUnavailable('Git 命令失败、超时或输出不可解析；需检查实际残留。') from exc
        if proc.returncode:
            raise GitVerificationUnavailable('Git: '+proc.stderr.strip()[:2000])
        return proc.stdout.strip()

    def records(self):
        if not self._path('store.lock').parent.exists(): return []
        result=[]
        for p in sorted(self.root.glob('*.json')):
            local_id(p.stem,'operation')
            row=read_json(self.workspace,self._path(p.name))
            if row.get('schema_version')!=1 or row.get('operation_id')!=p.stem or row.get('status') not in {'preparing','ready','partial','failed','removed'}:
                raise StorageError('Worktree 日志结构损坏。')
            safe_name(row['name']);local_id(row['task_id'],'task')
            expected=self.workspace/'.mini-claude-code/checkouts'/row['name']
            if row['path']!=str(expected) or row['branch']!='mini/'+row['name']:
                raise StorageError('Worktree 日志路径或分支不匹配。')
            result.append(row)
        return result

    def registrations(self):
        raw=self.git('-c','core.quotePath=false','worktree','list','--porcelain')
        rows=[]
        for record in raw.split('\n\n'):
            fields={}
            for line in record.splitlines():
                key,_,value=line.partition(' ')
                if key:
                    if key=='worktree' and value.startswith(chr(34)):
                        import ast
                        value=ast.literal_eval(value)
                    fields[key]=value
            if fields: rows.append(fields)
        return rows

    def validate(self, record):
        binding=record.worktree
        if binding is None:return self.workspace
        if binding['status']!='ready':raise TaskError('Worktree 尚未 ready 或绑定已失效，禁止回退主目录。')
        safe_name(binding['name']);local_id(binding['operation_id'],'operation')
        path=self.workspace/'.mini-claude-code/checkouts'/binding['name']
        safe_path(self.workspace,path)
        if binding['path']!=str(path) or binding['branch']!='mini/'+binding['name'] or not path.is_dir() or path.resolve()!=path:
            raise TaskError('Worktree 目录或绑定不匹配。')
        saved=read_json(self.workspace,self._path(binding['operation_id']+'.json'))
        if any(saved.get(k)!=v for k,v in binding.items()) or saved.get('task_id')!=record.task_id:
            raise TaskError('Worktree 操作日志与任务版本不一致。')
        registered=next((r for r in self.registrations() if r.get('worktree')==str(path)),None)
        if not registered or registered.get('branch')!='refs/heads/'+binding['branch']:
            raise TaskError('Git 注册或期望分支已变化。')
        if self.git('rev-parse','--show-toplevel',cwd=path)!=str(path) or self.git('symbolic-ref','--short','HEAD',cwd=path)!=binding['branch']:
            raise TaskError('Worktree 身份已变化。')
        if self.git('rev-parse',binding['base_commit']+'^{commit}')!=binding['base_commit']:
            raise TaskError('Worktree base commit 无效。')
        return path

    def create(self, name, task_id, base_ref, source, *, team_id=None):
        safe_name(name)
        if type(base_ref) is not str or not base_ref or base_ref.startswith('-') or '\0' in base_ref:
            raise TaskError('base_ref 必须为非选项 Git 引用。')
        if self.git('rev-parse','--show-toplevel')!=str(self.workspace):
            raise TaskError('canonical workspace 必须为 Git 仓库根目录。')
        commit=self.git('rev-parse','--verify','--end-of-options',base_ref+'^{commit}')
        path=self.workspace/'.mini-claude-code/checkouts'/name
        branch='mini/'+name
        with file_lock(self.workspace,self._path('operations.lock')):
            safe_path(self.workspace,path)
            if path.exists() or any(r.get('worktree')==str(path) for r in self.registrations()):
                raise TaskError('目标路径已存在或已登记，不能复用。')
            if self.git('branch','--list',branch):raise TaskError('分支已存在，不能复用。')
            if any(r['name']==name for r in self.records()):raise TaskError('名称已有历史操作记录。')
            operation_id=new_id('operation')
            binding={'operation_id':operation_id,'status':'preparing','name':name,'path':str(path),'branch':branch,'base_commit':commit}
            row={'schema_version':1,**binding,'task_id':task_id,'created_at':utc_now(),'error':None}
            # Intent is durable before either authority can claim success.
            atomic_json(self.workspace,self._path(operation_id+'.json'),row)
            with self.tasks._locked():
                record=self.tasks._get(self.tasks._read_all(),task_id)
                if record.status!='pending' or record.owner or record.worktree is not None or (team_id is not None and record.team_id!=team_id):
                    row.update(status='failed',error='任务不能绑定');atomic_json(self.workspace,self._path(operation_id+'.json'),row)
                    raise TaskError('仅可绑定本范围内 pending、未认领、未绑定的任务。')
                preparing=replace(record,worktree=binding,revision=record.revision+1,updated_at=utc_now(),last_modified_by=source)
                self.tasks._write(preparing)
            try:
                path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
                self.git('worktree','add','-b',branch,str(path),commit)
                registered=next((r for r in self.registrations() if r.get('worktree')==str(path)),None)
                if not registered or registered.get('branch')!='refs/heads/'+branch or registered.get('HEAD')!=commit or path.resolve()!=path:
                    raise TaskError('创建后 Git 注册校验失败。')
                row['status']='ready'
                atomic_json(self.workspace,self._path(operation_id+'.json'),row)
                with self.tasks._locked():
                    current=self.tasks._get(self.tasks._read_all(),task_id)
                    if current.revision!=preparing.revision or current.worktree!=binding:raise TaskError('创建期间任务版本变化。')
                    self.tasks._write(replace(current,worktree={k:row[k] for k in binding},revision=current.revision+1,updated_at=utc_now()))
                return row
            except Exception as exc:
                row.update(status='partial' if path.exists() or self._branch_exists(branch) else 'failed',error=str(exc)[:2000])
                atomic_json(self.workspace,self._path(operation_id+'.json'),row)
                with self.tasks._locked():
                    current=self.tasks._get(self.tasks._read_all(),task_id)
                    if current.worktree and current.worktree['operation_id']==operation_id:
                        self.tasks._write(replace(current,worktree={k:row[k] for k in binding},revision=current.revision+1,updated_at=utc_now()))
                raise TaskError(f'Worktree 创建未完整提交：{operation_id}；状态 {row["status"]}；残留保留。{exc}') from exc

    def _branch_exists(self, branch):
        try:return bool(self.git('branch','--list',branch))
        except TaskError:return True

    def recover(self):
        """Inspect incomplete transactions against Git; never infer ready from a directory."""
        warnings=[]
        rows=self.records()
        if not rows:return warnings
        with file_lock(self.workspace,self._path('operations.lock')):
            for row in self.records():
                if row['status'] in {'removed','partial','failed'}:continue
                record=self.tasks.snapshot().get(row['task_id'])
                binding=record.worktree if record else None
                if row['status']=='ready' and binding and binding['status']=='ready':
                    try:self.validate(record);continue
                    except GitVerificationUnavailable as exc:
                        warnings.append(f'Worktree {row["name"]}：Git 当前无法校验，保留已提交的 ready 记录；'
                            f'当前不确认可执行，认领或执行前必须重新校验。{exc}')
                        continue
                    except TaskError as exc:error=str(exc)
                else:error='上次创建事务未完整提交；不会自动恢复为 ready。'
                try:
                    registrations=self.registrations()
                    actual=next((r for r in registrations if r.get('worktree')==row['path']),None)
                except TaskError as exc:actual=None;error+=' Git 检查失败：'+str(exc)
                row.update(status='partial' if actual or Path(row['path']).exists() else 'failed',
                    error=error[:2000],observed_registration=actual)
                atomic_json(self.workspace,self._path(row['operation_id']+'.json'),row)
                with self.tasks._locked():
                    current=self.tasks._read_all().get(row['task_id'])
                    if current and current.worktree and current.worktree['operation_id']==row['operation_id']:
                        self.tasks._write(replace(current,worktree={**current.worktree,'status':row['status']},
                            revision=current.revision+1,updated_at=utc_now()))
        return warnings

    def remove(self,name):
        safe_name(name)
        with file_lock(self.workspace,self._path('operations.lock')):
            row=next((r for r in self.records() if r['name']==name),None)
            if row is None or row['status']!='ready':raise TaskError('仅可移除正常 ready checkout。')
            # Acquire task lock only for a snapshot; no Git or lease wait under it.
            record=self.tasks._get(self.tasks.snapshot(),row['task_id'])
            if record.status!='completed':raise TaskError('任务尚未完成。')
            lease=DirectoryLease(self.workspace,Path(row['path']),{'kind':'manual_remove'})
            try:
                path=self.validate(record)
                if self.git('status','--porcelain','--untracked-files=all','--ignored',cwd=path):
                    raise TaskError('checkout 包含修改、未跟踪或忽略文件，拒绝自动移除。')
                self.git('worktree','remove',str(path))
                row['status']='removed';atomic_json(self.workspace,self._path(row['operation_id']+'.json'),row)
                with self.tasks._locked():
                    current=self.tasks._get(self.tasks._read_all(),record.task_id)
                    self.tasks._write(replace(current,worktree={**current.worktree,'status':'removed'},revision=current.revision+1,updated_at=utc_now()))
                return row
            finally:lease.close()

    def register(self,registry,runtime):
        def create(args,context):
            row=self.create(args['name'],args['task_id'],args['base_ref'],runtime.tasks.source(),team_id=runtime.team_id)
            return ToolOutput(json.dumps(row,ensure_ascii=False)+'\nHEAD base excludes uncommitted main-workspace changes; no automatic merge.')
        # Invocation source belongs to this handler's host TaskService.
        create.set_invocation = runtime.tasks.set_invocation
        class Handler:
            set_invocation=staticmethod(runtime.tasks.set_invocation)
            def __call__(self,args,context):return create(args,context)
        handler=Handler()
        registry.register(ToolSpec('create_worktree','Bind an unowned pending task to a new Git checkout. Does not copy dirty main files or merge changes.',{
            'type':'object','properties':{'name':{'type':'string','minLength':1,'maxLength':64},'task_id':{'type':'string'},'base_ref':{'type':'string','default':'HEAD'}},
            'required':['name','task_id'],'additionalProperties':False}),handler.__call__)
