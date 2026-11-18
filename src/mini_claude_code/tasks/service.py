"""Six task tools and host-owned assignment validation; no scheduler."""

from dataclasses import replace
from contextlib import nullcontext
import json

from ..core.cancellation import CancellationToken, Cancelled
from ..core.errors import StorageError, ContractError
from ..core.messages import ToolSpec
from ..storage.session_store import SessionStore, durable_section
from ..storage.locking import SessionLock
from ..storage.task_store import TaskStore, authorize_completion, view
from ..tools.registry import ToolOutput
from .models import Assignment, TaskError

TASK_TOOLS = frozenset({'create_task','update_task','list_tasks','get_task','claim_task','complete_task'})


class TaskService:
    def __init__(self, runtime, parent=None):
        self.runtime = runtime
        self.parent = parent
        self.store = TaskStore(runtime.config.workspace)
        self.assignment = None
        self.invocation = None
        from ..worktrees.service import WorktreeService
        self.worktrees = WorktreeService(runtime.config.workspace, self.store)
        self.lease = None
        self.finished_batch = False
        self.last_completed = None
        self.last_root = None
        self.release_blocked = False

    def set_invocation(self, call, assistant_message_id):
        self.invocation = (call, assistant_message_id) if call is not None else None

    def source(self, kind='tool'):
        context = self.runtime.context
        if kind == 'tool':
            if self.invocation is None:
                raise TaskError('任务修改必须由宿主工具调用身份驱动。')
            call, message_id = self.invocation
        else:
            call, message_id = None, None
        return {'agent_id':context.agent_id, 'session_id':context.session_id,
                'assistant_message_id':message_id,'call_id':call.call_id if call else None,'kind':kind}

    @property
    def linked_assignment(self):
        return self.parent.linked_assignment if self.parent is not None else self.assignment

    def context(self, cancellation=None):
        if self.runtime.events is not None and self.runtime.events.background.failed:
            raise StorageError('后台输出保存失败，不能继续新的副作用。')
        if (self.runtime.cleanup_uncertain or self.runtime.shell.unconfirmed_work or self.runtime.shell._active
                or self.runtime.task_runner is not None and self.runtime.task_runner.cleanup_failed):
            self.runtime.cleanup_uncertain = True
            raise StorageError('所属资源未确认停止，不能继续任务或新的副作用。')
        assignment = self.linked_assignment
        if self.lease is not None:self.lease.verify()
        if assignment is not None:
            graph = self.store.snapshot(cancellation)
            record = graph.get(assignment.task_id)
            if record is None or record.status != 'in_progress' or record.owner != assignment.owner or record.worktree != assignment.binding:
                if cancellation is not None:
                    cancellation.cancel()
                    raise Cancelled()
                raise ContractError('当前任务认领已失效；旧 assignment 不能继续执行。')
        root = self.worktrees.validate(record) if assignment is not None else self.runtime.config.workspace
        if assignment is not None and str(root)!=assignment.cwd: raise ContractError('任务执行目录与 assignment 不匹配。')
        self.runtime.paths.effective_root = root if assignment is not None else None
        self.runtime.context = replace(self.runtime.context, task_id=assignment.task_id if assignment else None,
                                       cwd=assignment.cwd if assignment else str(self.runtime.config.workspace))
        if hasattr(self.runtime,'skills') and self.runtime.skills.context.cwd!=self.runtime.context.cwd:
            self.runtime.skills.context=self.runtime.context;self.runtime.skills.refresh()
        return self.runtime.context

    def restore_notice(self):
        # Never bind disk ownership to a new process-local assignment.
        records = self.store.snapshot()
        leftover = [r for r in records.values() if r.owner and r.owner['session_id']==self.runtime.state.session_id]
        for record in leftover:
            self.runtime.display('notice',
                f'遗留任务 {record.task_id}：owner={record.owner}；未恢复 assignment。'
                '请检查旧工作，先退出此会话，再用 tasks release 显式释放，然后重新认领。')

    def _output(self, record, *, metadata=None):
        return ToolOutput(json.dumps(record.to_dict(), ensure_ascii=False), metadata={
            'target_task_id':record.task_id, **(metadata or {})})

    def create(self, args, context):
        record = self.store.create(args['subject'], args['description'], self.source(), self.runtime.cancellation, team_id=self.runtime.team_id)
        return self._output(record)

    def update(self, args, context):
        record = self.store.add_dependencies(args['task_id'], args['add_blocked_by'],
                                             self.source(), self.runtime.cancellation)
        return self._output(record)

    def list(self, args, context):
        graph = self.store.snapshot(self.runtime.cancellation)
        items = [view(r, graph, brief=True) for r in graph.values()
                 if (args.get('status') is None or r.status==args['status']) and self.in_scope(r)]
        if args['ready_only']:
            items = [item for item in items if item['ready']]
        items.sort(key=lambda item:item['task_id'])
        offset, limit = args['offset'], args['limit']
        return ToolOutput(json.dumps({'tasks':items[offset:offset+limit], 'total':len(items),
            'offset':offset,'has_more':offset+limit<len(items)},ensure_ascii=False))

    def get(self, args, context):
        graph = self.store.snapshot(self.runtime.cancellation)
        record = self.store._get(graph, args['task_id'])
        if not self.in_scope(record): raise TaskError('任务不属于本团队。')
        return ToolOutput(json.dumps(view(record,graph),ensure_ascii=False),metadata={'target_task_id':record.task_id})

    def claim(self, args, context):
        if self.runtime.events is not None and self.runtime.events.background.has_running(unbound=True):
            raise TaskError('未绑定任务的后台命令仍运行，请先等待或取消。')
        if self.assignment is not None:
            raise TaskError('当前运行已经持有 assignment。')
        record = self.claim_host(args['task_id'], source=self.source())
        self.runtime.store.event('task_claimed', {'task_id':record.task_id,'owner':record.owner,'source':self.source()})
        return self._output(record, metadata={'task_id':record.task_id,'claim_id':record.owner['claim_id']})

    def complete(self, args, context):
        self.context(self.runtime.cancellation)
        source = self.source()
        team_id = self.runtime.team_id if self.runtime.teammate_spec is not None else None
        graph = self.store.snapshot(self.runtime.cancellation)
        target = self.store._get(graph, args['task_id'])
        # Reject unauthorized targets before touching the caller's active resources.
        # TaskStore repeats this check under its write lock before returning data.
        authorize_completion(target, self.assignment, source, team_id=team_id)
        if target.status != 'completed':
            if self.runtime.events is not None and self.runtime.events.background.has_running(args['task_id']):
                raise TaskError('该任务仍有后台命令运行，请等待或取消后再完成。')
            if self.runtime.task_runner is not None and self.runtime.task_runner.active:
                raise TaskError('子会话仍运行，不能提前完成父任务。')
            if self.runtime.mcp is not None:
                if self.runtime.mcp.has_work(args['task_id']):raise TaskError('任务仍有在途或未确认 MCP 请求。')
                self.runtime.mcp.disconnect_all()
        with durable_section():
            record, ready, changed = self.store.complete(args['task_id'], self.assignment, args['summary'],
                                                         source, self.runtime.cancellation, team_id=team_id)
            if self.assignment is not None and self.assignment.task_id==record.task_id:
                self.last_completed=record
                self.last_root=context.cwd
                self.finished_batch=bool(self.runtime.teammate_spec is not None or record.worktree is not None)
                self.assignment = None
                self.runtime.context = replace(context, task_id=None, cwd=str(self.runtime.config.workspace))
        if changed:
            if self.runtime.teammate_spec is not None: self.runtime.team.assignment_changed(self.runtime,None)
            if not self.finished_batch: self.release_lease()
            self.runtime.store.event('task_completed', {'task_id':record.task_id,'source':self.source(),
                                                       'newly_ready':ready,'completed_by':record.completed_by})
        return ToolOutput(json.dumps({'task':record.to_dict(),'already_completed':not changed,
            'newly_ready':ready},ensure_ascii=False),metadata={'target_task_id':record.task_id,'already_completed':not changed})

    def release_current(self, reason):
        if self.parent is not None or self.assignment is None:
            return
        assignment = self.assignment
        graph = self.store.snapshot()
        record = graph.get(assignment.task_id)
        if record is None or record.owner != assignment.owner:
            raise TaskError('当前 claim 已变化，不能释放另一个执行者的认领。')
        with durable_section():
            record = self.store.release(record.task_id, assignment.owner, record.revision,
                                        self.source('host'), reason, auto_claim_blocked=self.release_blocked or (self.runtime.teammate_spec is not None and reason!='关闭会话'))
            self.assignment = None
            self.runtime.context = replace(self.runtime.context, task_id=None)
        self.release_lease()
        if self.runtime.teammate_spec is not None: self.runtime.team.assignment_changed(self.runtime,None)
        self.runtime.store.event('task_released', {'task_id':record.task_id,'reason':reason,
                                                 'claim_id':assignment.claim_id})
        self.runtime.display('notice', f'任务 {record.task_id} 已安全释放为 pending；已有修改保留。')

    def in_scope(self,record):
        return self.runtime.teammate_spec is None or record.team_id==self.runtime.team_id

    def claim_host(self,task_id,*,source=None,adopt=False,automatic=False):
        from ..worktrees.service import DirectoryLease
        if self.assignment is not None or self.finished_batch: raise TaskError('当前执行者尚未结束上一任务。')
        if self.runtime.events is not None and self.runtime.events.background.has_running(unbound=True):
            raise TaskError('主目录仍有未绑定后台工作。')
        if self.runtime.mcp is not None:self.runtime.mcp.disconnect_all()
        graph=self.store.snapshot();record=self.store._get(graph,task_id)
        root=self.worktrees.validate(record)  # Git is never run under the graph lock.
        source=source or self.source('host')
        previous_team_id=record.team_id
        acquired=None
        def prepare(current):
            nonlocal acquired
            if current.worktree!=record.worktree: raise TaskError('目录绑定版本已变化。')
            # No wait: acquisition fails immediately on a directory conflict.
            acquired=self.lease if self.lease is not None and self.lease.root==root else DirectoryLease(self.runtime.config.workspace,root,current.owner)
        try:
            recovery=(self.runtime.team.task_adoption(record)
                      if adopt and not automatic and self.runtime.teammate_spec is not None else nullcontext(None))
            with recovery as adopt_scope:
                with durable_section():
                    record=self.store.claim(task_id,source,self.runtime.cancellation,
                        team_id=self.runtime.team_id if self.runtime.teammate_spec is not None else None,
                        adopt_session=self.runtime.team.lead.state.session_id if adopt else None,
                        adopt_scope=adopt_scope,prepare=prepare,automatic=automatic)
                    self.last_root=None
                    self.lease=acquired
                    self.assignment=Assignment.from_record(record,str(root))
                    self.lease.assign(record.owner)
                    self.runtime.paths.effective_root=root
                    self.runtime.context=replace(self.runtime.context,task_id=record.task_id,cwd=str(root))
            if previous_team_id is not None and record.team_id != previous_team_id:
                self.runtime.store.event('task_team_adopted',{'task_id':record.task_id,
                    'previous_team_id':previous_team_id,'team_id':record.team_id,
                    'lead_session_id':self.runtime.team.lead.state.session_id,
                    'lead_agent_id':self.runtime.team.lead.context.agent_id})
            if self.runtime.teammate_spec is not None:self.runtime.team.assignment_changed(self.runtime,record)
            self.context()
            return record
        except BaseException:
            if self.store.failed:
                self.runtime.cleanup_uncertain=True
                self.lease=acquired
            if acquired is not None and self.assignment is None and not self.store.failed: acquired.close()
            raise

    def release_lease(self):
        if self.lease is not None:self.lease.close();self.lease=None

    def begin_turn(self):
        self.finished_batch=False

    def batch_boundary(self):
        if self.finished_batch:self.release_lease()

    def guard(self,name,context):
        from ..worktrees.service import DirectoryLease
        if self.finished_batch and name in {'write_file','edit_file','bash','read_file','glob','load_skill','task','claim_task'}:
            raise PermissionError('本批任务已完成；剩余项目调用被拒绝，须等待下一轮。')
        if self.runtime.team is not None:self.runtime.team.guard(self.runtime,name,context)
        if name in {'write_file','edit_file','bash','task','connect_mcp'} and self.linked_assignment is None and self.parent is None:
            # Keep the main directory leased through the whole tool batch and any BG command.
            if self.lease is None:self.lease=DirectoryLease(self.runtime.config.workspace,self.runtime.config.workspace,
                {'agent_id':context.agent_id,'session_id':context.session_id,'kind':'foreground'})

    def idle_boundary(self):
        if self.runtime.cleanup_uncertain or self.store.failed:return
        if self.runtime.mcp is not None and self.runtime.mcp.has_processes():return
        if self.assignment is None and not (self.runtime.events is not None and self.runtime.events.background.has_running()):
            self.release_lease()

    def register(self, registry):
        task_id = {'type':'string','minLength':1}
        definitions = [
            ('create_task','Create a persistent pending task; use the returned real ID before adding dependencies.',
             {'subject':{'type':'string','minLength':1,'maxLength':200},
              'description':{'type':'string','maxLength':8000,'default':''}},['subject'],self.create),
            ('update_task','Add dependencies to a pending unowned task; no arbitrary state updates.',
             {'task_id':task_id,'add_blocked_by':{'type':'array','minItems':1,'maxItems':50,'items':task_id}},
             ['task_id','add_blocked_by'],self.update),
            ('list_tasks','List persistent task metadata and derived ready/blocked state.',
             {'status':{'type':'string','enum':['pending','in_progress','completed']},
              'ready_only':{'type':'boolean','default':False},
              'offset':{'type':'integer','minimum':0,'default':0},
              'limit':{'type':'integer','minimum':1,'maximum':100,'default':50}},[],self.list),
            ('get_task','Read a persistent task, dependencies, owner, sources and completion report.',
             {'task_id':task_id},['task_id'],self.get),
            ('claim_task','Atomically claim a ready task for the actual host executor. One task per agent.',
             {'task_id':task_id},['task_id'],self.claim),
            ('complete_task','Current assignment owner reports completion; this does not certify tests or merge changes.',
             {'task_id':task_id,'summary':{'type':'string','maxLength':2000,'default':''}},
             ['task_id'],self.complete),
        ]
        for name, description, properties, required, handler in definitions:
            registry.register(ToolSpec(name,description,{'type':'object','properties':properties,
                'required':required,'additionalProperties':False}),handler)


def release_orphan(store, task_id, *, interactive, confirm):
    """Explicit human recovery: session lock first, graph lock second, CAS on release."""
    if not interactive:
        raise TaskError('非交互环境无法确认孤立任务释放；请检查旧工作后在终端运行。')
    initial = store._get(store.snapshot(), task_id)
    if initial.status != 'in_progress':
        raise TaskError('指定任务没有活动认领。')
    owner = initial.owner
    session = SessionStore(store.workspace, owner['session_id'])
    metadata = session._metadata()
    if metadata['identity']['agent_id'] != owner['agent_id']:
        raise TaskError('任务负责人和会话身份不匹配，无法安全释放。')
    if not session._path('session.lock').is_file():
        raise StorageError('原会话锁文件缺失，无法判断旧运行时状态；请先检查存储。')
    lock = SessionLock(session._path('session.lock'))
    try:
        current = store._get(store.snapshot(), task_id)
        if current.owner != owner or current.revision != initial.revision:
            raise TaskError('检查期间任务版本已变化，请重新检查。')
        if confirm(current) is not True:
            raise TaskError('未确认释放；认领保持不变。')
        source = {'agent_id':owner['agent_id'],'session_id':owner['session_id'],
                  'assistant_message_id':None,'call_id':None,'kind':'manual'}
        return store.release(task_id, owner, initial.revision, source, '用户检查旧资源后明确释放孤立认领')
    finally:
        lock.close()
