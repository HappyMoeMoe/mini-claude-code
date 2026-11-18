"""Ordered internal message blocks and explicit JSON serialization."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal

from ._validation import (
    SCHEMA_VERSION, array, enum_value, envelope, fields, json_object,
    local_id, new_id, text, timestamp, utc_now,
)
from .errors import ContractError


class MessageRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class ToolStatus(str, Enum):
    SUCCESS = "success"
    ERROR = "error"
    DENIED = "denied"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class TextBlock:
    text: str
    type: Literal["text"] = field(default="text", init=False)

    def __post_init__(self) -> None:
        text(self.text, "text", empty=True)

    def to_dict(self) -> dict:
        return {"type": self.type, "text": self.text}

    @classmethod
    def from_dict(cls, data: dict) -> "TextBlock":
        fields(data, {"type", "text"})
        if data["type"] != "text":
            raise ContractError("Expected text block")
        return cls(text=data["text"])


@dataclass(frozen=True)
class ToolCall:
    call_id: str
    name: str
    arguments: dict
    type: Literal["tool_call"] = field(default="tool_call", init=False)

    def __post_init__(self) -> None:
        # Provider IDs are opaque association keys, never storage paths.
        text(self.call_id, "call_id")
        text(self.name, "name")
        json_object(self.arguments, "arguments")

    def to_dict(self) -> dict:
        json_object(self.arguments, "arguments")
        return {"type": self.type, "call_id": self.call_id, "name": self.name,
                "arguments": self.arguments.copy()}

    @classmethod
    def from_dict(cls, data: dict) -> "ToolCall":
        fields(data, {"type", "call_id", "name", "arguments"})
        if data["type"] != "tool_call":
            raise ContractError("Expected tool_call block")
        return cls(call_id=data["call_id"], name=data["name"], arguments=data["arguments"])


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    status: ToolStatus
    output: str
    metadata: dict = field(default_factory=dict)
    type: Literal["tool_result"] = field(default="tool_result", init=False)

    def __post_init__(self) -> None:
        text(self.call_id, "call_id")
        object.__setattr__(self, "status", enum_value(self.status, ToolStatus, "tool status"))
        text(self.output, "output", empty=True)
        json_object(self.metadata, "metadata")

    def to_dict(self) -> dict:
        json_object(self.metadata, "metadata")
        return {"type": self.type, "call_id": self.call_id, "status": self.status.value,
                "output": self.output, "metadata": self.metadata.copy()}

    @classmethod
    def from_dict(cls, data: dict) -> "ToolResult":
        fields(data, {"type", "call_id", "status", "output"}, {"metadata"})
        if data["type"] != "tool_result":
            raise ContractError("Expected tool_result block")
        return cls(call_id=data["call_id"], status=data["status"], output=data["output"],
                   metadata=data.get("metadata", {}))


ContentBlock = TextBlock | ToolCall | ToolResult


def block_from_dict(data: dict) -> ContentBlock:
    if type(data) is not dict:
        raise ContractError("Content block must be a JSON object")
    block_type = data.get("type")
    if block_type == "text":
        return TextBlock.from_dict(data)
    if block_type == "tool_call":
        return ToolCall.from_dict(data)
    if block_type == "tool_result":
        return ToolResult.from_dict(data)
    raise ContractError("Unsupported content block type")


def validate_content(content: list[ContentBlock], role: MessageRole) -> None:
    array(content, "content")
    allowed = {
        MessageRole.USER: (TextBlock,),
        MessageRole.ASSISTANT: (TextBlock, ToolCall),
        MessageRole.TOOL: (ToolResult,),
    }[role]
    ids = set()
    for block in content:
        if type(block) not in allowed:
            raise ContractError("Content block is not allowed for this role")
        if isinstance(block, (ToolCall, ToolResult)):
            if block.call_id in ids:
                raise ContractError("Duplicate call_id within one message")
            ids.add(block.call_id)


@dataclass(frozen=True)
class Message:
    role: MessageRole
    content: list[ContentBlock]
    message_id: str = field(default_factory=lambda: new_id("message"))
    created_at: str = field(default_factory=utc_now)
    origin: dict | None = None

    def __post_init__(self) -> None:
        local_id(self.message_id, "message")
        timestamp(self.created_at)
        object.__setattr__(self, "role", enum_value(self.role, MessageRole, "message role"))
        validate_content(self.content, self.role)
        if not self.content:
            raise ContractError("Message content must not be empty")
        if self.origin is not None:
            fields(self.origin, {'kind'}, {'event_ids','attempt_id'})
            if self.origin['kind'] not in {'human','background','cron','recovery','event','team'}:
                raise ContractError('Unknown message origin')
            if 'event_ids' in self.origin:
                array(self.origin['event_ids'], 'event_ids')
                for event_id in self.origin['event_ids']: local_id(event_id, 'event')
                if len(set(self.origin['event_ids'])) != len(self.origin['event_ids']):
                    raise ContractError('Duplicate event origin')
            if 'attempt_id' in self.origin: local_id(self.origin['attempt_id'], 'attempt')

    @property
    def is_human(self):
        return self.role == MessageRole.USER and (self.origin is None or self.origin['kind'] == 'human')

    def to_dict(self) -> dict:
        validate_content(self.content, self.role)
        return {"schema_version": SCHEMA_VERSION, "message_id": self.message_id,
                "role": self.role.value, "content": [block.to_dict() for block in self.content],
                "created_at": self.created_at, **({'origin':self.origin.copy()} if self.origin is not None else {})}

    @classmethod
    def from_dict(cls, data: dict) -> "Message":
        envelope(data, {"message_id", "role", "content", "created_at"}, {'origin'})
        return cls(message_id=data["message_id"], role=data["role"],
                   content=[block_from_dict(block) for block in array(data["content"], "content")],
                   created_at=data["created_at"], origin=data.get('origin'))


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict

    def __post_init__(self) -> None:
        text(self.name, "name")
        text(self.description, "description")
        json_object(self.input_schema, "input_schema")
        if self.input_schema.get("type") != "object":
            raise ContractError("Tool input_schema must describe an object")

    def to_dict(self) -> dict:
        json_object(self.input_schema, "input_schema")
        return {"name": self.name, "description": self.description,
                "input_schema": self.input_schema.copy()}

    @classmethod
    def from_dict(cls, data: dict) -> "ToolSpec":
        fields(data, {"name", "description", "input_schema"})
        return cls(name=data["name"], description=data["description"], input_schema=data["input_schema"])

