"""DeepSeek non-thinking Messages API, using the Anthropic SDK."""

import json
import math
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from types import SimpleNamespace

from ..config import Config
from ..core.errors import ContractError, ModelError
from ..core.messages import MessageRole, TextBlock, ToolCall, ToolStatus
from ..core.model import ModelRequest, ModelResponse, StopReason, TokenUsage, ModelStreamEvent


def request_messages(request: ModelRequest) -> list[dict]:
    from ..core.loop import validate_history

    validate_history(request.messages)
    messages = []
    for message in request.messages:
        content = []
        for block in message.content:
            if isinstance(block, TextBlock):
                content.append({"type": "text", "text": block.text})
            elif isinstance(block, ToolCall):
                content.append({"type": "tool_use", "id": block.call_id,
                                "name": block.name, "input": block.arguments})
            else:
                details = {"status": block.status.value}
                for key in ("exit_code", "timed_out", "truncated", "execution_unknown", "truncated_at_capture",
                            "artifact_id", "bytes_stored", "bytes_total", "offset", "char_offset", "limit",
                            "has_more", "next_offset", "next_char_offset", "task_id", "target_task_id", "resources_unconfirmed",
                            "background_id", "launch", "state", "job_id", "timezone", "durable"):
                    if key in block.metadata:
                        details[key] = block.metadata[key]
                content.append({"type": "tool_result", "tool_use_id": block.call_id,
                                "is_error": block.status != ToolStatus.SUCCESS,
                                # DeepSeek currently ignores is_error; retain explicit text status.
                                "content": f"[tool_result {json.dumps(details, allow_nan=False)}]\n{block.output}"})
        role = "user" if message.role == MessageRole.TOOL else message.role.value
        if messages and messages[-1]["role"] == role:
            messages[-1]["content"].extend(content)
        else:
            messages.append({"role": role, "content": content})
    return messages


def parse_response(response) -> ModelResponse:
    if response.role != "assistant" or response.type != "message":
        raise ContractError("Expected an assistant Messages response")
    reason = {"end_turn": StopReason.NORMAL, "stop_sequence": StopReason.NORMAL,
              "tool_use": StopReason.TOOL_CALL, "max_tokens": StopReason.OUTPUT_TRUNCATED}.get(response.stop_reason)
    if reason is None:
        raise ContractError("Unsupported or unsuccessful model stop_reason")
    usage = None
    provider_usage = getattr(response, "usage", None)
    if provider_usage is not None:
        usage = TokenUsage(input_tokens=getattr(provider_usage, "input_tokens", None),
                           output_tokens=getattr(provider_usage, "output_tokens", None))
    request_id = getattr(response, "_request_id", None) or response.id
    if reason == StopReason.OUTPUT_TRUNCATED:
        return ModelResponse([], reason, usage, request_id)
    content = []
    for block in response.content:
        if block.type == "text":
            content.append(TextBlock(block.text))
        elif block.type == "tool_use":
            content.append(ToolCall(block.id, block.name, block.input))
        else:
            raise ContractError("Only non-thinking text and client tool_use blocks are supported")
    return ModelResponse(content, reason, usage, request_id)


def _retry_after(headers):
    value = headers.get("retry-after")
    if value is None:
        return None
    try:
        delay = float(value)
    except ValueError:
        try:
            delay = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
    return min(30, max(0, delay)) if math.isfinite(delay) else None


def _arguments(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ContractError("Duplicate JSON key in streamed tool arguments")
        result[key] = value
    return result


class DeepSeekClient:
    def __init__(self, config: Config) -> None:
        config.require_model_ready()
        self._config = config
        self._client = None
        self._active_stream = None
        self._ensure_client()

    def _ensure_client(self):
        if self._client is not None:
            return
        config = self._config
        import httpx
        from anthropic import Anthropic

        self._client = Anthropic(api_key=config.api_key, auth_token=None, base_url=config.base_url,
                                 max_retries=0, timeout=httpx.Timeout(60, connect=10, write=30, pool=10))
        self._active_stream = None

    @contextmanager
    def _errors(self):
        import httpx
        from anthropic import APIConnectionError, APIError, APIStatusError, APITimeoutError

        try:
            yield
        except APITimeoutError as exc:
            raise ModelError("DeepSeek 请求超时。", retryable=True) from exc
        except (APIConnectionError, httpx.TransportError) as exc:
            raise ModelError("DeepSeek 连接中断；请检查 API 地址和网络。", retryable=True) from exc
        except APIStatusError as exc:
            status = exc.status_code
            body = getattr(exc, "body", None)
            error = body.get("error", {}) if isinstance(body, dict) else {}
            error_type = error.get("type") if isinstance(error, dict) else None
            code = error.get('code') if isinstance(error, dict) else None
            message = error.get('message', '') if isinstance(error, dict) else ''
            context_too_long = (code in {'context_length_exceeded', 'prompt_too_long'} if isinstance(code, str) else False)
            context_too_long |= error_type in {'context_length_exceeded', 'prompt_too_long'} if isinstance(error_type, str) else False
            if status in {400, 413} and isinstance(message, str):
                context_too_long |= any(phrase in message.lower() for phrase in (
                    'prompt is too long', 'maximum context length', 'context length exceeded',
                    'context window exceeded', 'input token count exceeds'))
            known_temporary = error_type in {"overloaded_error", "rate_limit_error", "api_error"} if isinstance(error_type, str) else False
            request_id = getattr(exc, "request_id", None)
            if isinstance(request_id, str):
                request_id = request_id.replace(self._client.api_key, "[redacted]")[:200]
            else:
                request_id = None
            hint = "；请求上下文超过服务端限制" if context_too_long else "；请检查配置、账户或服务状态"
            description = f"DeepSeek 流错误（{error_type}）。" if status == 200 and known_temporary else f"DeepSeek 返回 HTTP {status}{hint}。"
            raise ModelError(description, status_code=status,
                             retryable=known_temporary or status in {429, 500, 502, 503, 504, 529},
                             error_type=error_type if known_temporary else None, context_too_long=context_too_long,
                             provider_request_id=request_id, retry_after=_retry_after(exc.response.headers)) from exc
        except APIError as exc:
            raise ModelError("DeepSeek SDK 请求失败；请检查服务与协议配置。") from exc
        except (AttributeError, TypeError, ValueError, RecursionError) as exc:
            raise ContractError("DeepSeek 返回了不受支持或不完整的协议数据；未执行本次工具。") from exc

    @staticmethod
    def _payload(request):
        payload = {"model": request.model, "system": request.system,
                   "messages": request_messages(request), "max_tokens": request.max_output_tokens,
                   "thinking": {"type": "disabled"}}
        if request.tools:
            payload["tools"] = [tool.to_dict() for tool in request.tools]
        return payload

    def generate(self, request: ModelRequest) -> ModelResponse:
        self._ensure_client()
        with self._errors():
            response = self._client.messages.create(**self._payload(request), stream=False)
        try:
            return parse_response(response)
        except (AttributeError, TypeError, ValueError, RecursionError) as exc:
            raise ContractError("DeepSeek 响应不符合支持的协议；未执行本次工具。") from exc

    def stream(self, request):
        self._ensure_client()
        started = False
        stopped = False
        stop_reason = None
        request_id = None
        blocks, fragments, closed = [], {}, set()
        input_tokens = output_tokens = None
        with self._errors():
            raw = self._client.messages.create(**self._payload(request), stream=True)
            self._active_stream = raw
            try:
                for event in raw:
                    kind = event.type
                    if kind == "ping":
                        continue
                    if stopped:
                        raise ContractError("Unexpected event after message_stop")
                    if kind == "message_start":
                        if started or event.message.role != "assistant" or event.message.content:
                            raise ContractError("Invalid message_start")
                        started = True
                        request_id = event.message.id
                        usage = getattr(event.message, "usage", None)
                        input_tokens = getattr(usage, "input_tokens", None)
                        output_tokens = getattr(usage, "output_tokens", None)
                    elif not started:
                        raise ContractError("Stream missing message_start")
                    elif kind == "content_block_start":
                        if stop_reason is not None or event.index != len(blocks):
                            raise ContractError("Invalid content block index/order")
                        block = event.content_block
                        if block.type == "text":
                            blocks.append({"type": "text", "text": block.text})
                            if block.text:
                                yield ModelStreamEvent("text", text=block.text)
                        elif block.type == "tool_use":
                            blocks.append({"type": "tool_use", "id": block.id, "name": block.name, "input": block.input})
                            fragments[event.index] = ""
                        else:
                            raise ContractError("Unsupported streamed content block")
                    elif kind == "content_block_delta":
                        if event.index >= len(blocks) or event.index < 0 or event.index in closed:
                            raise ContractError("Delta has no open block")
                        block = blocks[event.index]
                        if event.delta.type == "text_delta" and block["type"] == "text":
                            block["text"] += event.delta.text
                            yield ModelStreamEvent("text", text=event.delta.text)
                        elif event.delta.type == "input_json_delta" and block["type"] == "tool_use":
                            fragments[event.index] += event.delta.partial_json
                        else:
                            raise ContractError("Unsupported streamed delta")
                    elif kind == "content_block_stop":
                        if event.index < 0 or event.index >= len(blocks) or event.index in closed:
                            raise ContractError("Invalid content_block_stop")
                        closed.add(event.index)
                    elif kind == "message_delta":
                        if len(closed) != len(blocks):
                            raise ContractError("Message ended with an open content block")
                        reason = getattr(event.delta, "stop_reason", None)
                        if reason is not None:
                            if stop_reason is not None:
                                raise ContractError("Duplicate model stop reason")
                            stop_reason = reason
                        usage = getattr(event, "usage", None)
                        if getattr(usage, "input_tokens", None) is not None:
                            input_tokens = usage.input_tokens
                        if getattr(usage, "output_tokens", None) is not None:
                            output_tokens = usage.output_tokens
                    elif kind == "message_stop":
                        if stop_reason is None or len(closed) != len(blocks):
                            raise ContractError("Incomplete message_stop")
                        stopped = True
                    else:
                        raise ContractError("Unknown model stream event")
                if not stopped:
                    raise ModelError("模型流提前结束；未执行本次工具。", retryable=True)
                if stop_reason != "max_tokens":
                    for index, fragment in fragments.items():
                        if fragment:
                            if blocks[index]["input"]:
                                raise ContractError("Ambiguous streamed input")
                            try:
                                blocks[index]["input"] = json.loads(fragment, object_pairs_hook=_arguments)
                            except (ValueError, RecursionError) as exc:
                                raise ContractError("Invalid streamed tool JSON") from exc
                response = SimpleNamespace(role="assistant", type="message", id=request_id, stop_reason=stop_reason,
                                           content=[SimpleNamespace(**block) for block in blocks],
                                           usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens))
                yield ModelStreamEvent("final", response=parse_response(response))
            finally:
                raw.close()
                self._active_stream = None

    def cancel_active(self):
        if self._active_stream is not None:
            self._active_stream.close()
            self._active_stream = None
        if self._client is not None:
            self._client.close()
            self._client = None

    def close(self):
        self.cancel_active()
