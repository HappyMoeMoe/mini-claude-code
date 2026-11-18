"""Serializable runtime envelopes and trusted notification provenance."""

from dataclasses import dataclass, field

from ._validation import (
    SCHEMA_VERSION, envelope, json_object, local_id, new_id, text, timestamp, utc_now,
)


@dataclass(frozen=True)
class RuntimeEvent:
    kind: str
    target_session_id: str
    payload: dict
    event_id: str = field(default_factory=lambda: new_id("event"))
    created_at: str = field(default_factory=utc_now)
    source_agent_id: str | None = None
    source_id: str | None = None
    occurrence_id: str | None = None

    def __post_init__(self) -> None:
        local_id(self.event_id, "event")
        text(self.kind, "kind")
        local_id(self.target_session_id, "session")
        timestamp(self.created_at)
        json_object(self.payload, "payload")
        if self.source_agent_id is not None:
            local_id(self.source_agent_id, "agent")
        if self.source_id is not None:
            text(self.source_id, 'source_id')
        if self.occurrence_id is not None:
            local_id(self.occurrence_id, 'occurrence')

    def to_dict(self) -> dict:
        json_object(self.payload, "payload")
        return {"schema_version": SCHEMA_VERSION, "event_id": self.event_id, "kind": self.kind,
                "target_session_id": self.target_session_id, "created_at": self.created_at,
                "payload": self.payload.copy(), "source_agent_id": self.source_agent_id,
                **({'source_id':self.source_id} if self.source_id is not None else {}),
                **({'occurrence_id':self.occurrence_id} if self.occurrence_id is not None else {})}

    @classmethod
    def from_dict(cls, data: dict) -> "RuntimeEvent":
        envelope(data, {"event_id", "kind", "target_session_id", "created_at", "payload"},
                 {"source_agent_id",'source_id','occurrence_id'})
        return cls(event_id=data["event_id"], kind=data["kind"], target_session_id=data["target_session_id"],
                   created_at=data["created_at"], payload=data["payload"], source_agent_id=data.get("source_agent_id"),
                   source_id=data.get('source_id'), occurrence_id=data.get('occurrence_id'))
