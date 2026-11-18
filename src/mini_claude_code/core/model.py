"""Synchronous model boundary; SDK conversion belongs to future providers."""

from dataclasses import dataclass
from enum import Enum
from typing import Protocol
from collections.abc import Iterator

from ._validation import SCHEMA_VERSION, array, enum_value, envelope, fields, integer, text, local_id
from .errors import ContractError
from .messages import ContentBlock, Message, MessageRole, ToolSpec, block_from_dict, validate_content


class StopReason(str, Enum):
    NORMAL = "normal"
    TOOL_CALL = "tool_call"
    OUTPUT_TRUNCATED = "output_truncated"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None

    def __post_init__(self) -> None:
        for name in ("input_tokens", "output_tokens"):
            if getattr(self, name) is not None:
                integer(getattr(self, name), name)

    def to_dict(self) -> dict:
        return {"input_tokens": self.input_tokens, "output_tokens": self.output_tokens}

    @classmethod
    def from_dict(cls, data: dict) -> "TokenUsage":
        fields(data, set(), {"input_tokens", "output_tokens"})
        return cls(input_tokens=data.get("input_tokens"), output_tokens=data.get("output_tokens"))


@dataclass(frozen=True)
class ModelRequest:
    system: str
    messages: list[Message]
    tools: list[ToolSpec]
    model: str
    max_output_tokens: int
    task_id: str | None = None
    tools_version: int | None = None
    tool_bindings: dict | None = None

    def __post_init__(self) -> None:
        text(self.system, "system", empty=True)
        text(self.model, "model")
        integer(self.max_output_tokens, "max_output_tokens", minimum=1)
        if self.tools_version is not None:integer(self.tools_version,"tools_version")
        if self.tool_bindings is not None:
            from ._validation import json_object
            json_object(self.tool_bindings,"tool_bindings")
        if self.task_id is not None:
            local_id(self.task_id, 'task')
        array(self.messages, "messages")
        array(self.tools, "tools")
        if any(type(message) is not Message for message in self.messages):
            raise ContractError("messages must contain Message objects")
        if any(type(tool) is not ToolSpec for tool in self.tools):
            raise ContractError("tools must contain ToolSpec objects")
        if len({tool.name for tool in self.tools}) != len(self.tools):
            raise ContractError("Duplicate tool name in model request")

    def to_dict(self) -> dict:
        self.__post_init__()
        return {"schema_version": SCHEMA_VERSION, "system": self.system,
                "messages": [message.to_dict() for message in self.messages],
                "tools": [tool.to_dict() for tool in self.tools],
                "model": self.model, "max_output_tokens": self.max_output_tokens,
                **({'task_id':self.task_id} if self.task_id is not None else {}),
                **({'tools_version':self.tools_version,'tool_bindings':self.tool_bindings} if self.tools_version is not None else {})}

    @classmethod
    def from_dict(cls, data: dict) -> "ModelRequest":
        envelope(data, {"system", "messages", "tools", "model", "max_output_tokens"}, {'task_id','tools_version','tool_bindings'})
        return cls(system=data["system"], model=data["model"], max_output_tokens=data["max_output_tokens"], task_id=data.get('task_id'), tools_version=data.get('tools_version'), tool_bindings=data.get('tool_bindings'),
                   messages=[Message.from_dict(message) for message in array(data["messages"], "messages")],
                   tools=[ToolSpec.from_dict(tool) for tool in array(data["tools"], "tools")])


@dataclass(frozen=True)
class ModelResponse:
    content: list[ContentBlock]
    stop_reason: StopReason
    usage: TokenUsage | None = None
    provider_request_id: str | None = None

    def __post_init__(self) -> None:
        validate_content(self.content, MessageRole.ASSISTANT)
        object.__setattr__(self, "stop_reason", enum_value(self.stop_reason, StopReason, "stop reason"))
        if self.usage is not None and type(self.usage) is not TokenUsage:
            raise ContractError("usage must be TokenUsage or None")
        if self.provider_request_id is not None:
            text(self.provider_request_id, "provider_request_id")

    def to_dict(self) -> dict:
        self.__post_init__()
        return {"schema_version": SCHEMA_VERSION, "content": [block.to_dict() for block in self.content],
                "stop_reason": self.stop_reason.value,
                "usage": self.usage.to_dict() if self.usage is not None else None,
                "provider_request_id": self.provider_request_id}

    @classmethod
    def from_dict(cls, data: dict) -> "ModelResponse":
        envelope(data, {"content", "stop_reason"}, {"usage", "provider_request_id"})
        usage = data.get("usage")
        return cls(content=[block_from_dict(block) for block in array(data["content"], "content")],
                   stop_reason=data["stop_reason"], usage=TokenUsage.from_dict(usage) if usage is not None else None,
                   provider_request_id=data.get("provider_request_id"))


@dataclass(frozen=True)
class ModelStreamEvent:
    kind: str
    text: str = ""
    response: ModelResponse | None = None


class ModelClient(Protocol):
    def generate(self, request: ModelRequest) -> ModelResponse:
        """Return an internal response or raise ModelError; do not execute tools."""
        ...


    def stream(self, request: ModelRequest) -> Iterator[ModelStreamEvent]:
        """Yield provisional text and exactly one final response."""
        ...
