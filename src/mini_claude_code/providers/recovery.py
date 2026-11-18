"""Bounded model-request recovery; never wraps tool execution."""

import random
from dataclasses import replace

from ..core.errors import ModelError, ContractError
from ..core.model import ModelResponse, StopReason


class RequestRecovery:
    def __init__(self, client, config, cancellation, *, stream=True, display=None, event=None, wait=None):
        self.client, self.config, self.cancellation = client, config, cancellation
        self.stream_enabled = stream
        self.display = display or (lambda kind, value: None)
        self.event = event or (lambda kind, payload: None)
        self.wait = wait or cancellation.wait
        self.current_model = config.model

    def generate(self, request, *, reactive=None):
        tokens = request.max_output_tokens
        raised_budget = False
        overloaded = 0
        compacted = False
        for attempt in range(1, 4):
            self.cancellation.check()
            current = replace(request, model=self.current_model, max_output_tokens=tokens)
            self.event("model_attempt", {"attempt": attempt, "model": current.model, "max_output_tokens": tokens})
            delay = 0
            try:
                if self.stream_enabled:
                    final = None
                    iterator = self.client.stream(current)
                    try:
                        for item in iterator:
                            self.cancellation.check()
                            if final is not None:
                                raise ContractError("Stream emitted events after its final response")
                            if item.kind == "text":
                                self.display("delta", item.text)
                            elif item.kind == "final" and type(item.response) is ModelResponse:
                                final = item.response
                            else:
                                raise ContractError("Unknown model stream event")
                    finally:
                        iterator.close()
                    if final is None:
                        raise ModelError("模型流未完成；本次响应不执行工具。", retryable=True)
                    response = final
                else:
                    response = self.client.generate(current)
                self.cancellation.check()
                response.to_dict()
                overloaded = 0
                if response.stop_reason != StopReason.OUTPUT_TRUNCATED:
                    return response
                if raised_budget or attempt == 3:
                    raise ContractError("模型响应再次截断或预算已耗尽；未执行本次响应中的工具。")
                tokens = self.config.max_recovery_output_tokens
                raised_budget = True
                self.event("output_budget_raised", {"attempt": attempt, "max_output_tokens": tokens})
            except ModelError as exc:
                if exc.context_too_long:
                    if reactive is not None and not compacted and attempt < 3:
                        compacted = True
                        request = reactive(current)
                        self.event('reactive_compact', {'attempt': attempt})
                        self.display('notice', '服务端报告上下文过长，已做一次受控压缩后重试。')
                        continue
                    raise
                overloaded = overloaded + 1 if (exc.status_code in {429, 503, 529} or exc.error_type in {"overloaded_error", "rate_limit_error"}) else 0
                if not exc.retryable or attempt == 3:
                    raise
                if overloaded >= 2 and self.config.fallback_model:
                    self.current_model = self.config.fallback_model
                    self.display("notice", f"本轮切换备用模型：{self.current_model}")
                delay = min(30, 0.5 * 2 ** (attempt - 1) + random.uniform(0, 0.1))
                if exc.retry_after is not None:
                    delay = min(30, max(0, exc.retry_after))
                self.event("model_retry", {"attempt": attempt, "status_code": exc.status_code,
                                           "delay_seconds": delay, "model": self.current_model})
            self.display("notice", "上次输出未完成，正在重试。")
            self.wait(delay)
        raise ContractError("Model retry budget exhausted")
