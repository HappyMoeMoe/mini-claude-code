"""Single-session assembly, durable lifecycle, signals and resource ownership."""

import platform
import os
import signal
import threading
from dataclasses import replace

from .config import Config, configure_profile
from .context.manager import ContextManager
from .context.memory import MemoryService
from .storage.artifacts import ArtifactStore
from .context.skills import SkillLoader
from .context.prompt import PromptBuilder
from .tasks.todo import TodoManager
from .tasks.service import TaskService
from .agents.factory import AgentSessionFactory
from .agents.subagent import SubagentRunner
from .core._validation import new_id
from .core.cancellation import CancellationToken, Cancelled
from .core.errors import ConfigurationError, StorageError
from .core.loop import AgentLoop, TurnOutcome
from .core.messages import ToolCall
from .core.redaction import redact_arguments
from .core.session import AgentIdentity, AgentRole, ExecutionContext, SessionState, SessionStatus
from .hooks.manager import HookManager
from .permissions.policy import PathPolicy, PermissionPolicy, RestrictedPolicy
from .providers.deepseek import DeepSeekClient
from .storage.session_store import SessionStore, durable_section
from .tools.executor import ToolExecutor
from .tools.filesystem import register_files
from .tools.registry import ToolRegistry
from .tools.shell import ShellRunner, register_shell


class Runtime:
    def __init__(self, config: Config, *, interactive: bool, approve=None, display=None,
                 max_rounds=30, resume=None, stream=True, hooks=None, client=None, wait=None,
                 child_spec=None, parent_runtime=None, child_client_factory=None, client_factory=None, teams=False, teammate_spec=None, mcp_config=None, mcp_parent=None, mcp_connection_factory=None):
        config=configure_profile(config,minimal=config.minimal,disabled=config.disabled_features,
                                 teams=teams,mcp_config=mcp_config)
        config.require_model_ready()
        if platform.system() != "Linux":
            raise ConfigurationError("当前版本仅支持 Linux。")
        self.config = config
        self.child_spec, self.parent_runtime = child_spec, parent_runtime
        self.teammate_spec = teammate_spec
        self.team = teammate_spec.manager if teammate_spec is not None else None
        self.team_id = self.team.team_id if self.team else None
        self.teams_enabled = teams
        self.client_factory = client_factory or DeepSeekClient
        self.mcp = None
        self.mcp_approve = None
        self.task_runner = None
        self.tasks = None
        self.cleanup_uncertain = False
        self._display = display or (lambda kind, value: None)
        self.client = self.shell = self.store = None
        self._previous_signals = {}
        self.cancellation = None
        self.events = None
        self.automatic = False
        self.owner_thread = threading.get_ident()
        try:
            self.shell = ShellRunner()
            self.paths = PathPolicy(config.workspace)
            self.registry = ToolRegistry()
            register_files(self.registry, self.paths)
            register_shell(self.registry, self.shell, allow_background=child_spec is None and teammate_spec is None and "background" not in config.disabled_features,
                           expose_background="background" not in config.disabled_features)
            self.hooks = hooks or HookManager(warn=lambda text: self.display("notice", text))
            self.executor = ToolExecutor(self.registry, self.paths, PermissionPolicy(), approve, self.hooks)
            self.executor.allow_background = child_spec is None and teammate_spec is None and "background" not in config.disabled_features
            if resume is not None:
                self.store = SessionStore.resume(config.workspace, resume)
                self.state = self.store.state
                self.display("notice", f"已恢复 {resume}：{len(self.state.messages)} 条消息；等待新的输入。")
                if self.store.model != config.model:
                    self.display("notice", f"模型配置变更：{self.store.model} → {config.model}")
                for warning in self.store.warnings:
                    self.display("notice", warning)
                self.store.model = config.model
            else:
                identity = teammate_spec.identity if teammate_spec is not None else child_spec.identity if child_spec is not None else AgentIdentity(new_id("agent"), AgentRole.LEAD)
                self.state = SessionState(teammate_spec.session_id if teammate_spec else new_id("session"), identity, str(config.workspace))
                self.store = SessionStore.create(config.workspace, self.state, config.model,
                                                 linkage=teammate_spec.linkage() if teammate_spec else child_spec.linkage() if child_spec else None)
            self.context = ExecutionContext(self.state.session_id, self.state.identity.agent_id, str(config.workspace), interactive,
                                            task_id=parent_runtime.context.task_id if parent_runtime else None)
            self.tasks = TaskService(self, parent=parent_runtime.tasks if parent_runtime else None)
            if self.context.task_id is not None:
                self.store.event('task_associated', {'task_id':self.context.task_id,'parent_session_id':parent_runtime.state.session_id})
            self.executor.context_provider = self.tasks.context
            self.executor.guard = self.tasks.guard
            self.executor.task_id_provider = lambda:self.context.task_id
            if resume is not None:
                for warning in self.tasks.worktrees.recover():self.display('notice',warning)
                self.tasks.restore_notice()
            self.todos = TodoManager(self.state)
            self.skills = SkillLoader(self.paths, self.context)
            if "skills" not in config.disabled_features:self.skills.register(self.registry)
            self.artifacts = ArtifactStore(self.store)
            self.artifacts.register(self.registry)
            self.executor.result_transform = self.artifacts.transform
            self.shell.output_sink = self.artifacts.shell_sink
            self.shell.capture_check = self.artifacts.check_capture
            self.shell.capture_closed = self.artifacts.capture_closed
            if child_spec is not None:
                self.skills.catalog = parent_runtime.skills.catalog
                self.registry.retain(child_spec.allowed_tools)
                self.executor.policy = RestrictedPolicy(parent_runtime.executor.policy, child_spec.allowed_tools)
            elif teammate_spec is None:
                if not config.minimal:self.todos.register(self.registry)
                if "tasks" not in config.disabled_features:self.tasks.register(self.registry)
                if "subagents" not in config.disabled_features:
                    self.task_runner = SubagentRunner(self, AgentSessionFactory(self, child_client_factory))
                    self.task_runner.register(self.registry)
            if teammate_spec is not None:self.tasks.register(self.registry)
            self.client = client or self.client_factory(config)
            limiter=self.team or (parent_runtime.team if parent_runtime else None)
            if limiter is not None:
                with limiter.clients_lock:
                    if any(self.client is c for c in limiter.clients):
                        self.client=None
                        raise ConfigurationError('每个会话必须创建独立模型客户端。')
                    limiter.clients.append(self.client)
                from .teams.manager import LimitedClient
                self.client = LimitedClient(self.client,limiter.semaphore,lambda:self.cancellation)
            self.memory = MemoryService(config, self.client, session_store=self.store, artifacts=self.artifacts, display=self.display,
                                        wait=wait, parent=parent_runtime.memory if parent_runtime else self.team.lead.memory if teammate_spec else None,
                                        task_id_provider=lambda:self.context.task_id)
            self.context_manager = ContextManager(self.store, self.artifacts, self.client, config, display=self.display,
                                                   wait=wait, task_id_provider=lambda:self.context.task_id)
            self.context_manager.register(self.registry)
            if child_spec is not None:
                self.registry.retain(child_spec.allowed_tools)
            if teammate_spec is not None:
                from copy import deepcopy
                self.memory.selected=deepcopy(self.team.lead.memory.selected)
            system = PromptBuilder(self.state.identity, self.context, self.todos, self.skills,
                                   mode=child_spec.mode if child_spec else None, memory=self.memory, tasks=self.tasks)
            self.loop = AgentLoop(self.client, self.executor, system, config.model, max_rounds, self.display,
                                  store=self.store, config=config, stream=stream, wait=wait,
                                  context_manager=self.context_manager, memory=self.memory)
            if child_spec is None and teammate_spec is None and not config.minimal:
                from .runtime_events import RuntimeEvents
                self.events = RuntimeEvents(self)
                if "scheduler" not in config.disabled_features:self.events.cron.register(self.registry)
                if "background" not in config.disabled_features:self.shell.background_handler = self.events.background.launch
                self.artifacts.external_usage = self.events.background.usage
                self.context_manager.goal_ids_provider = lambda:self.events.goal_ids
                self.loop.boundary = self.events.boundary
                if resume is not None:
                    from .teams.store import TeamStore
                    interrupted=TeamStore.interrupt_old(config.workspace,self.state.session_id)
                    if interrupted:self.display('notice',f'{interrupted} 个旧队友标记 interrupted；未恢复线程或批准，请检查遗留 claim。')
                    self.events.recover()
                if teams:
                    from .teams.manager import TeamManager, LimitedClient
                    self.team=TeamManager(self,child_client_factory or self.client_factory);self.team_id=self.team.team_id
                    self.client=LimitedClient(self.client,self.team.semaphore,lambda:self.cancellation)
                    self.loop.client=self.context_manager.client=self.memory.client=self.client
                    self.team.register(self.registry,self)
                    self.tasks.worktrees.register(self.registry,self)
            if teammate_spec is not None:
                from .teams.manager import TEAMMATE_TOOLS
                self.team.register(self.registry,self)
                self.registry.retain(TEAMMATE_TOOLS)
                self.executor.allow_background=False
                self.loop.boundary=lambda:self.team.boundary(self)
                self.executor.boundary=self.loop.boundary
                self.loop.end_work=lambda:self.tasks.finished_batch or self.team.member(self.context.agent_id)['plan_status']=='pending'
            if mcp_config is not None:
                from .mcp.config import load_mcp_config,MCPConfig
                from .mcp.manager import MCPManager
                parsed=mcp_config if isinstance(mcp_config,MCPConfig) else load_mcp_config(mcp_config)
                kwargs={'connection_factory':mcp_connection_factory} if mcp_connection_factory else {}
                self.mcp=MCPManager(self,parsed,mcp_parent,**kwargs)
                self.mcp.register();self.executor.dynamic_manager=self.mcp
            # A boundary is reached after the complete accepted tool batch has been paired.
            previous=self.loop.boundary
            def boundary():
                if previous is not None:previous()
                self.tasks.context(self.cancellation)
                if self.mcp is not None:self.mcp.sync()
                self.tasks.batch_boundary()
                if self.teammate_spec is None:self.tasks.finished_batch=False
            self.loop.boundary=boundary
        except BaseException:
            self.close()
            raise

    def redact_arguments(self, arguments):
        secrets = [self.config.api_key]
        if self.mcp is not None:
            for server in self.mcp.config.servers.values():
                secrets.extend(os.environ.get(source) for source in server.env_from.values())
        return redact_arguments(arguments, secrets)

    def display(self, kind, value):
        if kind == 'call' and isinstance(value, ToolCall):
            value = replace(value, arguments=self.redact_arguments(value.arguments))
        self._display(kind, value)

    def __enter__(self):
        if self.events is not None:self.events.start()
        if threading.current_thread() is threading.main_thread():
            self._previous_signals[signal.SIGTERM] = signal.getsignal(signal.SIGTERM)
            signal.signal(signal.SIGTERM, self._terminate)
        return self

    @staticmethod
    def _terminate(signum, frame):
        raise SystemExit(128 + signum)

    def run(self, prompt, cancellation=None, *, input_messages=None, event_origin=None, accepted_callback=None):
        self.assert_owner()
        if self.cleanup_uncertain:
            raise StorageError('上次执行链未确认停止，不能继续新工作；请检查资源与任务认领。')
        token = cancellation if cancellation is not None else CancellationToken()
        outcome = None
        try:
            self.cancellation = token
            self.shell.cancellation = token
            token.check()
            if self.events is not None:self.events.start()
            self.tasks.begin_turn()
            self.tasks.context(token)
            if self.child_spec is None and "skills" not in self.config.disabled_features:
                self.skills.context=self.context
                self.skills.refresh()
                for warning in self.skills.catalog.warnings:
                    self.display("notice", warning)
            if self.task_runner is not None:self.task_runner.begin_turn()
            token.check()
            outcome = self.loop.run(self.state, self.context, prompt, token, automatic=self.automatic,
                                    input_messages=input_messages,event_origin=event_origin,accepted_callback=accepted_callback)
            return outcome
        except (KeyboardInterrupt, Cancelled):
            token.cancel()
            outcome = TurnOutcome.CANCELLED
            if hasattr(self.client,'cancel_active'):self.client.cancel_active()
            return outcome
        finally:
            try:
                if outcome is None or outcome.value not in {'completed','denied'}:
                    self._stop_and_release('执行轮失败或取消' if outcome is None else outcome.value)
            finally:
                try:self.tasks.idle_boundary()
                finally:
                    self.shell.cancellation = None
                    self.cancellation = None

    def assert_owner(self):
        if threading.get_ident()!=self.owner_thread:
            raise StorageError('只有所属会话执行线程可以提交历史或运行模型。')

    def run_automatic(self, messages, event_ids, attempt_id, accepted):
        from .runtime_events import AUTO_TOOLS
        if self.state.status != SessionStatus.IDLE:raise ConfigurationError('自动事件只能在会话空闲时执行。')
        interactive = self.context.interactive
        self.automatic = True
        self.context = replace(self.context,interactive=False)
        from .teams.manager import COORDINATION
        self.executor.allowed_tools = AUTO_TOOLS | (COORDINATION if self.team is not None else frozenset())
        self.executor.allow_background = False
        try:
            return self.run('',input_messages=messages,event_origin={'kind':'event','event_ids':event_ids,'attempt_id':attempt_id},
                            accepted_callback=accepted)
        finally:
            self.automatic = False
            self.context = replace(self.context,interactive=interactive)
            self.executor.allowed_tools = None
            self.executor.allow_background = True

    def _stop_and_release(self, reason):
        with durable_section():
            try:
                if self.mcp is not None:
                    try:self.mcp.close(permanent=reason=='关闭会话')
                    except Exception:self.cleanup_uncertain=True
                if self.events is not None:self.events.close_background(permanent=reason=='关闭会话')
                if self.shell is not None:
                    try:
                        self.shell.close()
                    except BaseException:
                        self.cleanup_uncertain = True
                        raise
                if (self.cleanup_uncertain or self.shell is not None and (self.shell._active or self.shell.unconfirmed_work)
                        or self.task_runner is not None and (self.task_runner.active or self.task_runner.cleanup_failed)):
                    self.cleanup_uncertain = True
                    raise StorageError('所属执行链未确认停止。')
                if self.tasks is None or self.tasks.assignment is None or self.tasks.parent is not None:
                    if self.tasks is not None:self.tasks.release_lease()
                    return True
                if self.store is None or self.store.failed or self.tasks.store.failed:
                    raise StorageError('关键存储状态不确定。')
                self.tasks.release_current(reason)
                return True
            except Exception:
                if self.shell is not None and self.shell._active:
                    self.cleanup_uncertain = True
                self.display('notice', '未确认安全释放；保留任务认领，请检查任务文件、会话来源和旧资源。')
                return False

    def compact(self, focus=''):
        self.assert_owner()
        if self.mcp is not None:self.mcp.sync()
        if self.state.status != SessionStatus.IDLE:
            raise ConfigurationError('只能在空闲时手动压缩。')
        token = CancellationToken()
        tools = [t for t in self.registry.specs() if self.executor.visible(self.registry.get(t.name))]
        system = self.loop.system.build(tools)
        return self.context_manager.prepare(system, tools, self.config.model, token, force=True, focus=focus)

    def close(self):
        self.assert_owner()
        error = None
        try:
            with durable_section():
                if self.team is not None and self.teammate_spec is None:
                    try:self.team.close()
                    except Exception as exc:error=exc
                if self.events is not None:
                    try:self.events.close_scheduler()
                    except Exception as exc:self.cleanup_uncertain=True;error=exc
                if self.shell is not None:
                    if self._stop_and_release('关闭会话') is False:
                        error = error or StorageError('关闭时后台结果或资源未能可靠收尾；请检查保存的记录。')
        finally:
            try:
                if self.client is not None:
                    self.client.close()
            finally:
                try:
                    if self.store is not None and not self.store.failed:
                        self.state.status = SessionStatus.CLOSED
                        self.store.save_metadata()
                finally:
                    if self.store is not None:
                        self.store.close()
                        self.store = None
                    if self.events is not None:self.events.wakeup.close()
                    for signum, handler in self._previous_signals.items():
                        signal.signal(signum, handler)
                    self._previous_signals.clear()
        if error is not None:raise StorageError('会话已停止，但收尾状态不确定；未自动释放不安全的任务认领。') from error

    def __exit__(self, exc_type, exc, traceback):
        self.close()
