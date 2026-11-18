"""One synchronous loop shared by model-independent sessions."""

from collections.abc import Callable
from enum import Enum

from .errors import ContractError
from .messages import Message, MessageRole, TextBlock, ToolCall, ToolResult
from .model import ModelClient, ModelRequest, ModelResponse, StopReason
from .session import ExecutionContext, SessionState, SessionStatus
from ..tools.executor import ToolExecutor


class TurnOutcome(str, Enum):
    COMPLETED = "completed"
    LIMIT_REACHED = "limit_reached"
    CANCELLED = "cancelled"
    DENIED = "denied"


def validate_history(messages: list[Message], *, allow_pending: bool = False) -> set[str]:
    pending = set()
    accepted = set()
    message_ids = set()
    for message in messages:
        message.to_dict()
        if message.message_id in message_ids:
            raise ContractError("Duplicate message ID")
        message_ids.add(message.message_id)
        if message.role == MessageRole.TOOL:
            for result in message.content:
                if result.call_id not in pending:
                    raise ContractError("Orphaned or duplicate tool result")
                pending.remove(result.call_id)
        else:
            if pending:
                raise ContractError("Tool calls must be paired before another message")
            for block in message.content:
                if isinstance(block, ToolCall):
                    if block.call_id in accepted:
                        raise ContractError("Tool call IDs cannot be reused in this session")
                    accepted.add(block.call_id)
                    pending.add(block.call_id)
    if pending and not allow_pending:
        raise ContractError("Unpaired tool calls in model request")
    return accepted


def validate_response(response: ModelResponse, accepted: set[str]) -> list[ToolCall]:
    if type(response) is not ModelResponse:
        raise ContractError("Model returned an unsupported response object")
    response.to_dict()
    if response.stop_reason == StopReason.OUTPUT_TRUNCATED:
        raise ContractError("模型输出被截断；未接受或执行本次响应中的任何工具。")
    calls = [block for block in response.content if isinstance(block, ToolCall)]
    if response.stop_reason not in {StopReason.NORMAL, StopReason.TOOL_CALL}:
        raise ContractError("Unknown model stop reason")
    if bool(calls) != (response.stop_reason == StopReason.TOOL_CALL):
        raise ContractError("Model stop reason and tool calls disagree")
    if not response.content:
        raise ContractError("Model returned an empty response")
    if any(call.call_id in accepted for call in calls):
        raise ContractError("Model reused an existing call ID")
    return calls


class AgentLoop:
    def __init__(self, client: ModelClient, executor: ToolExecutor, system: str,
                 model: str, max_rounds: int = 30, display=None, *, store=None,
                 config=None, stream=True, wait=None, context_manager=None, memory=None):
        from types import SimpleNamespace
        if type(max_rounds) is not int or max_rounds < 1:
            raise ValueError("max_rounds must be a positive integer")
        self.client, self.executor = client, executor
        self.system, self.model, self.max_rounds = system, model, max_rounds
        self.display = display or (lambda kind, value: None)
        self.store, self.stream, self.wait = store, stream, wait
        self.context_manager = context_manager
        self.memory = memory
        self.current_turn_id = None
        self.boundary = None
        self.end_work = None
        self.config = config or SimpleNamespace(model=model, fallback_model=None,
                                                max_output_tokens=8000, max_recovery_output_tokens=16000)

    def _append(self, state, message):
        if self.store is not None:
            self.store.append_message(message)
        else:
            state.messages.append(message)

    def run(self, state, context, prompt, cancellation=None, *, automatic=False, input_messages=None,
            event_origin=None, accepted_callback=None):
        from ..hooks.manager import HookAction
        from ..providers.recovery import RequestRecovery
        from ._validation import new_id
        from .cancellation import CancellationToken, Cancelled
        token = cancellation or CancellationToken()
        hooks = self.executor.hooks
        if state.status != SessionStatus.IDLE:
            raise ContractError("Session is not idle")
        if (state.session_id, state.identity.agent_id) != (context.session_id, context.agent_id):
            raise ContractError("Execution identity does not match this session")
        validate_history(state.messages)
        turn_id = new_id("turn")
        try:
            decision = None if automatic else hooks.trigger("UserPromptSubmit", {"turn_id": turn_id, "prompt": prompt,
                                                          "context": context.to_dict()})
        except KeyboardInterrupt:
            token.cancel()
            return TurnOutcome.CANCELLED
        if decision is not None and decision.action == HookAction.DENY:
            self.display("notice", decision.reason or "本次输入被 Hook 拒绝。")
            return TurnOutcome.DENIED
        outcome = "failed"
        intended = set()
        def event(kind, payload):
            if self.store is not None:
                task_id = self.executor.task_id_provider() if self.executor.task_id_provider is not None else context.task_id
                self.store.event(kind, {"turn_id": turn_id, **({'task_id':task_id} if task_id else {}), **payload})
        recovery = RequestRecovery(self.client, self.config, token, stream=self.stream,
                                   display=self.display, event=event, wait=self.wait)
        state.status = SessionStatus.RUNNING
        self.current_turn_id = turn_id
        turn_start = len(state.messages)
        accepted_events = False
        try:
            if automatic:
                known = {m.message_id for m in state.messages}
                for message in input_messages:
                    if message.message_id not in known:
                        self._append(state, message); known.add(message.message_id)
            else:
                self._append(state, Message(MessageRole.USER, [TextBlock(prompt)], origin={'kind':'human'}))
            event("turn_start", {'automatic':automatic})
            if self.store is not None:
                self.store.save_metadata()
            if self.memory is not None and not automatic:
                self.memory.begin_turn(state, turn_id, token)
            elif self.memory is not None:
                self.memory.refresh()
            for _ in range(self.max_rounds):
                token.check()
                if self.boundary is not None:self.boundary()
                context = self.executor.resolve_context(context, token)
                accepted = validate_history(state.messages)
                tools = self.executor.registry.specs()
                tools = [tool for tool in tools if self.executor.visible(self.executor.registry.get(tool.name))]
                version,snapshot=self.executor.registry.snapshot([tool.name for tool in tools])
                if hasattr(self.system, 'prepare'):
                    system = self.system.prepare(tools, state.messages, self.config.context_budget_chars)
                else:
                    system = self.system.build(tools) if hasattr(self.system, "build") else self.system
                manager = self.context_manager
                current_view = None
                if manager is not None:
                    force = manager.pending_focus is not None
                    focus = manager.pending_focus or ''
                    manager.pending_focus = None
                    current_view = manager.prepare(system, tools, recovery.current_model, token, force=force, focus=focus)
                request = ModelRequest(system, current_view.messages if current_view else state.messages.copy(), tools,
                                       recovery.current_model, self.config.max_output_tokens, task_id=context.task_id,
                    tools_version=version if self.executor.dynamic_manager else None,
                    tool_bindings={n:t.binding.summary() for n,t in snapshot.items() if t.binding is not None} if self.executor.dynamic_manager else None)
                def reactive_compact(failed_request):
                    nonlocal current_view
                    current_view = manager.prepare(system, tools, recovery.current_model, token, reactive=True)
                    from dataclasses import replace
                    return replace(failed_request, messages=current_view.messages)
                response = recovery.generate(request, reactive=reactive_compact if manager is not None else None)
                if state.identity.role.value == "subagent" and response.stop_reason == StopReason.NORMAL and not response.content:
                    # Preserve an empty final response without inventing a report.
                    from dataclasses import replace
                    response = replace(response, content=[TextBlock("")])
                calls = validate_response(response, accepted)
                assistant = Message(MessageRole.ASSISTANT, response.content, origin=event_origin if automatic else None)
                self._append(state, assistant)
                if automatic and not accepted_events:
                    accepted_callback(assistant); accepted_events = True
                if self.context_manager is not None:
                    self.context_manager.accepted(current_view)
                if self.stream:
                    self.display("stream_end", None)
                for block in response.content:
                    if isinstance(block, TextBlock) and not self.stream:
                        self.display("text", block.text)
                    elif isinstance(block, ToolCall):
                        self.display("call", block)
                def intent(call):
                    event("tool_intent", {"assistant_message_id": assistant.message_id, "call_id": call.call_id,
                                          "tool_name": call.name})
                    intended.add((assistant.message_id, call.call_id))
                def commit(result):
                    self._append(state, Message(MessageRole.TOOL, [result]))
                self.executor.request_tools=snapshot if self.executor.dynamic_manager else None
                for call in calls:
                    result = self.executor.execute(call, context, cancellation=token, intent=intent, commit=commit, assistant_message_id=assistant.message_id)
                    self.display("result", result)
                if token.cancelled:
                    raise Cancelled()
                validate_history(state.messages)
                if self.boundary is not None:self.boundary()
                if self.end_work is not None and self.end_work():
                    outcome = "completed"
                    return TurnOutcome.COMPLETED
                if not calls:
                    outcome = "completed"
                    if self.memory is not None and not automatic:
                        self.memory.after_turn(state, turn_id, state.messages[turn_start:], token)
                    return TurnOutcome.COMPLETED
            outcome = "limit_reached"
            return TurnOutcome.LIMIT_REACHED
        except (KeyboardInterrupt, Cancelled):
            token.cancel()
            if hasattr(self.client, "cancel_active"):
                self.client.cancel_active()
            completed = {b.call_id for m in state.messages for b in m.content if isinstance(b, ToolResult)}
            if self.store is not None:
                intended.update((e.payload.get("assistant_message_id"), e.payload.get("call_id"))
                                for e in self.store.events if e.kind == "tool_intent")
            for message in state.messages.copy():
                for call in message.content:
                    if not isinstance(call, ToolCall) or call.call_id in completed:
                        continue
                    unknown = (message.message_id, call.call_id) in intended
                    result = ToolResult(call.call_id, "error" if unknown else "cancelled",
                                        "取消时执行状态不确定，请检查实际状态。" if unknown else "开始执行前已取消。",
                                        {"tool_name": call.name, "execution_unknown": unknown, "executed": unknown})
                    commit = Message(MessageRole.TOOL, [result])
                    self._append(state, commit)
                    hooks.trigger("PostToolUse", {"call": call.to_dict(), "result": result.to_dict(),
                                                  "context": context.to_dict()})
            validate_history(state.messages)
            outcome = "cancelled"
            return TurnOutcome.CANCELLED
        finally:
            self.executor.request_tools=None
            self.current_turn_id = None
            state.status = SessionStatus.IDLE
            try:
                if self.store is not None and not self.store.failed:
                    event("turn_end", {"outcome": outcome})
                    self.store.save_metadata()
            finally:
                hooks.trigger("Stop", {"turn_id": turn_id, "outcome": outcome, "session_id": state.session_id,
                                       'automatic':automatic})
