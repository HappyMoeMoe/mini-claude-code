"""Unique execution path: hooks, forced authorization, intent, result, post-hook."""

from collections.abc import Callable

from ..core.cancellation import Cancelled, CancellationToken
from ..core.errors import StorageError
from ..core.messages import ToolCall, ToolResult, ToolStatus
from ..core.session import ExecutionContext
from ..hooks.manager import HookAction, HookManager
from ..permissions.policy import Decision, PathPolicy, PermissionPolicy
from .registry import ToolRegistry

Approval = Callable[[ToolCall, dict, ExecutionContext], bool]
PREVIEW_LIMIT = 50_000


class ToolExecutor:
    def __init__(self, registry: ToolRegistry, paths: PathPolicy, policy: PermissionPolicy,
                 approve: Approval | None = None, hooks: HookManager | None = None):
        self.registry, self.paths, self.policy, self.approve = registry, paths, policy, approve
        self.hooks = hooks or HookManager()
        self.result_transform = None
        self.context_provider = None
        self.task_id_provider = None
        self.allowed_tools = None
        self.allow_background = True
        self.guard = None
        self.boundary = None
        self.dynamic_manager = None
        self.request_tools = None

    def visible(self,tool):
        if tool.binding is not None:
            return self.dynamic_manager is not None and self.dynamic_manager.visible(tool.binding)
        return self.allowed_tools is None or tool.spec.name in self.allowed_tools

    def resolve_context(self, context, cancellation):
        return self.context_provider(cancellation) if self.context_provider is not None else context

    def execute(self, call, context, *, cancellation=None, intent=None, commit=None, assistant_message_id=None):
        token = cancellation or CancellationToken()
        metadata = {"tool_name": call.name, "executed": False}
        try:
            token.check()
            if self.boundary is not None: self.boundary()
            context = self.resolve_context(context, token)
            tool = self.registry.get(call.name)
            expected = self.request_tools.get(call.name) if self.request_tools is not None else tool
            if self.dynamic_manager is not None and self.request_tools is not None and expected is None:
                raise ValueError('工具未在本次模型请求快照中公开；新发现工具从下一请求可用。')
            if expected is not None and expected.binding is not None:
                tool=expected
                metadata.update(tool.binding.summary())
                self.dynamic_manager.check_binding(tool.binding)
            if self.allowed_tools is not None and call.name not in self.allowed_tools and (tool is None or tool.binding is None):
                raise PermissionError('自动事件轮未授予该工具。')
            if context.task_id is not None:
                metadata['task_id'] = context.task_id
            if (tool is None or tool.binding is None) and hasattr(self.policy, "permits_tool") and not self.policy.permits_tool(call.name):
                raise PermissionError("Tool is not granted to this subagent")
            if tool is None:
                raise ValueError("Unknown tool")
            args = tool.validate(call.arguments,cancellation=token)
            if call.name == 'bash' and args.get('run_in_background') and not self.allow_background:
                raise PermissionError('本执行者不能启动后台 Shell。')
            if self.guard is not None: self.guard(call.name, context)
            self.paths.cwd(context)
            if call.name in {"read_file", "write_file", "edit_file"}:
                args["path"] = str(self.paths.resolve(args["path"], context))
            elif call.name == "glob":
                self.paths.validate_pattern(args["pattern"])
            hook = self.hooks.trigger("PreToolUse", {"call": call.to_dict(), "arguments": args,
                                                     "context": context.to_dict()})
            if hook.action == HookAction.DENY:
                raise PermissionError(hook.reason or "PreToolUse denied")
            token.check()
            decision = (self.dynamic_manager.authorize(tool.binding,args,context) if tool.binding is not None
                        else self.policy.decide(call.name, context))
            if decision == Decision.DENY:
                raise PermissionError("Tool denied by host policy")
            if decision == Decision.ASK:
                if not context.interactive or (self.approve is None and (tool.binding is None or self.dynamic_manager.runtime.mcp_approve is None)):
                    raise PermissionError("Interactive approval is unavailable; command was not executed")
                approved = (self.dynamic_manager.approve(tool.binding,call,args.copy(),context) if tool.binding is not None
                            else self.approve(call, args.copy(), context))
                if approved is not True:
                    raise PermissionError("User denied this call; command was not executed")
            token.check()
            if intent is not None:
                intent(call)
            token.check()
            # Approval may have waited; validate the claim again before any side effect.
            context = self.resolve_context(context, token)
            if self.guard is not None: self.guard(call.name, context)
            if tool.binding is not None:self.dynamic_manager.authorize(tool.binding,args,context)
            metadata["executed"] = True
            owner = getattr(tool.handler, "__self__", None)
            if call.name == "task" and hasattr(owner, "current_call_id"):
                owner.current_call_id = call.call_id
            if hasattr(owner, "set_invocation"):
                owner.set_invocation(call, assistant_message_id)
            try:
                output = tool.handler(args, context)
            finally:
                if call.name == "task" and hasattr(owner, "current_call_id"):
                    owner.current_call_id = None
                if hasattr(owner, "set_invocation"):
                    owner.set_invocation(None, None)
            metadata.update(output.metadata or {})
            result = self._result(call, output.status, output.output, metadata)
        except StorageError:
            raise
        except (KeyboardInterrupt, Cancelled):
            token.cancel()
            metadata["execution_unknown"] = metadata["executed"]
            result = self._result(call, ToolStatus.ERROR if metadata["executed"] else ToolStatus.CANCELLED,
                                  "已取消；已开始的操作可能产生了副作用，请检查实际状态。" if metadata["executed"] else "执行前已取消。", metadata)
        except PermissionError as exc:
            result = self._result(call, ToolStatus.DENIED, str(exc), metadata)
        except Exception as exc:
            result = self._result(call, ToolStatus.ERROR, f"{type(exc).__name__}: {exc}", metadata)
        # Outside the tool error boundary: failed storage must never become a tool error.
        if self.result_transform is not None:
            result = self.result_transform(result, call, assistant_message_id)
        if commit is not None:
            commit(result)
        self.hooks.trigger("PostToolUse", {"call": call.to_dict(), "result": result.to_dict(),
                                          "context": context.to_dict()})
        return result

    def _result(self, call, status, output, metadata):
        if self.result_transform is None and len(output) > PREVIEW_LIMIT:
            notice = "\n[truncated at 50,000 characters; full output is not retained]"
            output = output[:PREVIEW_LIMIT - len(notice)] + notice
            metadata["truncated"] = True
        return ToolResult(call.call_id, status, output, metadata)
