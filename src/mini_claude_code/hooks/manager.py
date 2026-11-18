"""Ordered hooks with copied inputs and explicit decisions."""

from copy import deepcopy
from dataclasses import dataclass
from enum import Enum


class HookAction(str, Enum):
    CONTINUE = "continue"
    DENY = "deny"


@dataclass(frozen=True)
class HookDecision:
    action: HookAction = HookAction.CONTINUE
    reason: str = ""

    def __post_init__(self):
        object.__setattr__(self, "action", HookAction(self.action))
        if type(self.reason) is not str:
            raise ValueError("Hook reason must be text")


class HookManager:
    EVENTS = {"UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"}

    def __init__(self, warn=None):
        self._callbacks = {event: [] for event in self.EVENTS}
        self.warn = warn or (lambda message: None)

    def register(self, event, callback):
        if event not in self.EVENTS or not callable(callback):
            raise ValueError("Unknown hook event or invalid callback")
        self._callbacks[event].append(callback)

    def trigger(self, event, payload):
        before = event in {"UserPromptSubmit", "PreToolUse"}
        for callback in self._callbacks[event]:
            try:
                result = callback(deepcopy(payload))
                if type(result) is not HookDecision:
                    raise ValueError("Hooks must return HookDecision")
            except Exception:
                if before:
                    return HookDecision(HookAction.DENY, "前置 Hook 失败，已拒绝本次操作。")
                self.warn(f"{event} Hook 失败；已提交结果保持不变。")
                continue
            if before and result.action == HookAction.DENY:
                return result
        return HookDecision()
