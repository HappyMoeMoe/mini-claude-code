"""Serializable session state and host-owned execution identity."""

from dataclasses import dataclass, field
from enum import Enum
from pathlib import PurePosixPath

from ._validation import SCHEMA_VERSION, array, enum_value, envelope, fields, local_id, text
from .errors import ContractError
from .messages import Message


class AgentRole(str, Enum):
    LEAD = "lead"
    SUBAGENT = "subagent"
    TEAMMATE = "teammate"


class SessionStatus(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    CLOSED = "closed"


def absolute_path(value: object, name: str) -> None:
    """Validate representation only; filesystem authorization is a later layer."""
    text(value, name)
    if "\0" in value or not PurePosixPath(value).is_absolute() or ".." in PurePosixPath(value).parts:
        raise ContractError(f"{name} must be an absolute normalized Linux path string")


@dataclass(frozen=True)
class AgentIdentity:
    agent_id: str
    role: AgentRole
    parent_agent_id: str | None = None

    def __post_init__(self) -> None:
        local_id(self.agent_id, "agent")
        object.__setattr__(self, "role", enum_value(self.role, AgentRole, "agent role"))
        if self.parent_agent_id is not None:
            local_id(self.parent_agent_id, "agent")
            if self.parent_agent_id == self.agent_id:
                raise ContractError("An agent cannot be its own parent")

    def to_dict(self) -> dict:
        return {"agent_id": self.agent_id, "role": self.role.value,
                "parent_agent_id": self.parent_agent_id}

    @classmethod
    def from_dict(cls, data: dict) -> "AgentIdentity":
        fields(data, {"agent_id", "role"}, {"parent_agent_id"})
        return cls(agent_id=data["agent_id"], role=data["role"],
                   parent_agent_id=data.get("parent_agent_id"))


@dataclass
class SessionState:
    session_id: str
    identity: AgentIdentity
    workspace: str
    messages: list[Message] = field(default_factory=list)
    status: SessionStatus = SessionStatus.IDLE

    def __post_init__(self) -> None:
        local_id(self.session_id, "session")
        if type(self.identity) is not AgentIdentity:
            raise ContractError("identity must be an AgentIdentity")
        absolute_path(self.workspace, "workspace")
        array(self.messages, "messages")
        if any(type(message) is not Message for message in self.messages):
            raise ContractError("messages must contain Message objects")
        if len({message.message_id for message in self.messages}) != len(self.messages):
            raise ContractError("Duplicate message_id in session")
        self.status = enum_value(self.status, SessionStatus, "session status")

    def to_dict(self) -> dict:
        # Mutable session state is revalidated when it leaves the runtime.
        self.__post_init__()
        return {"schema_version": SCHEMA_VERSION, "session_id": self.session_id,
                "identity": self.identity.to_dict(), "workspace": self.workspace,
                "messages": [message.to_dict() for message in self.messages], "status": self.status.value}

    @classmethod
    def from_dict(cls, data: dict) -> "SessionState":
        envelope(data, {"session_id", "identity", "workspace", "messages", "status"})
        return cls(session_id=data["session_id"], identity=AgentIdentity.from_dict(data["identity"]),
                   workspace=data["workspace"], status=data["status"],
                   messages=[Message.from_dict(message) for message in array(data["messages"], "messages")])


@dataclass(frozen=True)
class ExecutionContext:
    session_id: str
    agent_id: str
    cwd: str
    interactive: bool
    task_id: str | None = None

    def __post_init__(self) -> None:
        local_id(self.session_id, "session")
        local_id(self.agent_id, "agent")
        absolute_path(self.cwd, "cwd")
        if type(self.interactive) is not bool:
            raise ContractError("interactive must be a boolean")
        if self.task_id is not None:
            local_id(self.task_id, "task")

    def to_dict(self) -> dict:
        return {"schema_version": SCHEMA_VERSION, "session_id": self.session_id,
                "agent_id": self.agent_id, "cwd": self.cwd,
                "interactive": self.interactive, "task_id": self.task_id}

    @classmethod
    def from_dict(cls, data: dict) -> "ExecutionContext":
        envelope(data, {"session_id", "agent_id", "cwd", "interactive"}, {"task_id"})
        return cls(session_id=data["session_id"], agent_id=data["agent_id"], cwd=data["cwd"],
                   interactive=data["interactive"], task_id=data.get("task_id"))

