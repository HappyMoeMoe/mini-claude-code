"""Single-writer JSONL sessions with durable intents and conservative recovery."""

import json
import os
import tempfile
import signal
import threading
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from ..core._validation import envelope, local_id, utc_now, timestamp, integer
from ..core.errors import ContractError, StorageError
from ..core.events import RuntimeEvent
from ..core.messages import Message, MessageRole, ToolCall, ToolResult, ToolStatus
from ..core.session import AgentIdentity, SessionState, SessionStatus
from .locking import SessionLock


@contextmanager
def durable_section():
    """Defer SIGINT until the durable write and matching memory update agree."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT})
    try:
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)


def safe_path(workspace: Path, path: Path) -> Path:
    try:
        parts = path.relative_to(workspace).parts
    except ValueError as exc:
        raise StorageError("存储路径超出 workspace。") from exc
    current = workspace
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise StorageError("运行数据路径不能使用符号链接。")
    return path


def sessions_root(workspace):
    return safe_path(workspace, workspace / ".mini-claude-code" / "sessions")


class SessionStore:
    def __init__(self, workspace: Path, session_id: str):
        try:
            local_id(session_id, "session")
        except ContractError as exc:
            raise StorageError("会话 ID 格式无效。") from exc
        self.workspace = workspace.resolve(strict=True)
        self.path = sessions_root(self.workspace) / session_id
        safe_path(self.workspace, self.path)
        self.lock = None
        self.failed = False
        self.state = None
        self.model = None
        self.created_at = utc_now()
        self.warnings = []
        self.events = []
        self.linkage = {}

    def _path(self, name):
        return safe_path(self.workspace, self.path / name)

    def _check(self):
        if self.failed or self.lock is None:
            raise StorageError("会话存储不可用；已停止新操作。")

    def _append(self, name, data):
        self._check()
        try:
            payload = (json.dumps(data, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
            fd = os.open(self._path(name), os.O_APPEND | os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "ab") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        except (OSError, ValueError, UnicodeError, StorageError) as exc:
            self.failed = True
            raise StorageError("会话关键记录写入失败；停止新模型请求及工具执行。") from exc

    @classmethod
    def create(cls, workspace, state, model, *, linkage=None):
        store = cls(workspace, state.session_id)
        try:
            store.path.mkdir(parents=True, exist_ok=False, mode=0o700)
            store.lock = SessionLock(store._path("session.lock"))
            store.state, store.model = state, model
            store.linkage = dict(linkage or {})
            # Create both journals before publishing metadata.
            for name in ("messages.jsonl", "events.jsonl"):
                store._append_empty(name)
            store.save_metadata()
            return store
        except (OSError, StorageError) as exc:
            store.close()
            raise StorageError("无法创建会话存储。") from exc

    def _append_empty(self, name):
        fd = os.open(self._path(name), os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.flush()
            os.fsync(stream.fileno())

    def append_message(self, message):
        with durable_section():
            self._append("messages.jsonl", message.to_dict())
            self.state.messages.append(message)

    def event(self, kind, payload):
        event = RuntimeEvent(kind=kind, target_session_id=self.state.session_id,
                             source_agent_id=self.state.identity.agent_id, payload=payload)
        with durable_section():
            self._append("events.jsonl", event.to_dict())
            self.events.append(event)
        return event

    def save_metadata(self):
        self._check()
        name = None
        try:
            data = {"schema_version": 1, "session_id": self.state.session_id,
                    "identity": self.state.identity.to_dict(), "workspace": self.state.workspace,
                    "status": self.state.status.value, "model": self.model,
                    "created_at": self.created_at, "message_count": len(self.state.messages), **self.linkage}
            fd, name = tempfile.mkstemp(prefix=".metadata-", dir=self.path)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=False, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self._path("session.json"))
            directory = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except (OSError, ValueError, StorageError) as exc:
            self.failed = True
            raise StorageError("会话元数据写入失败。") from exc
        finally:
            if name is not None:
                Path(name).unlink(missing_ok=True)

    def _read_journal(self, name, decoder):
        path = self._path(name)
        data = path.read_bytes()
        records = []
        offset = 0
        for line in data.splitlines(keepends=True):
            terminated = line.endswith(b"\n")
            try:
                raw = json.loads(line)
            except (ValueError, UnicodeError) as exc:
                if terminated or offset + len(line) != len(data):
                    raise StorageError("会话日志中间损坏，拒绝恢复。") from exc
                backup = self._path(f"{name}.tail-{uuid4().hex}.bak")
                fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                directory = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
                directory = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
                with path.open("r+b") as stream:
                    stream.truncate(offset)
                    stream.flush()
                    os.fsync(stream.fileno())
                self.warnings.append(f"{name} 末尾断行已备份并修复。")
                break
            # A complete but invalid record is never treated as a torn append.
            records.append(decoder(raw))
            offset += len(line)
            if not terminated:
                with path.open("ab") as stream:
                    stream.write(b"\n")
                    stream.flush()
                    os.fsync(stream.fileno())
        return records

    @classmethod
    def resume(cls, workspace, session_id):
        from ..core.loop import validate_history

        store = cls(workspace, session_id)
        try:
            if not store.path.is_dir():
                raise StorageError("指定会话不存在。")
            store.lock = SessionLock(store._path("session.lock"))
            metadata = store._metadata()
            if metadata["identity"]["role"] != "lead":
                raise StorageError("子会话不能恢复为交互主会话；请恢复 parent_session_id 对应的父会话。")
            messages = store._read_journal("messages.jsonl", Message.from_dict)
            validate_history(messages, allow_pending=True)
            events = store._read_journal("events.jsonl", RuntimeEvent.from_dict)
            store.events = events
            if len({e.event_id for e in events}) != len(events):
                raise StorageError("事件 ID 重复。")
            if any(e.target_session_id != session_id for e in events):
                raise StorageError("事件归属不匹配。")
            store.state = SessionState(session_id, AgentIdentity.from_dict(metadata["identity"]),
                                       str(store.workspace), messages, SessionStatus.IDLE)
            store.model, store.created_at = metadata["model"], metadata["created_at"]
            call_sources = {(m.message_id, b.call_id) for m in messages for b in m.content if isinstance(b, ToolCall)}
            intents = {(e.payload.get("assistant_message_id"), e.payload.get("call_id"))
                       for e in events if e.kind == "tool_intent"}
            if not intents <= call_sources:
                raise StorageError("执行意图关联无效。")
            completed = {b.call_id for m in messages for b in m.content if isinstance(b, ToolResult)}
            for message in messages.copy():
                for call in message.content:
                    if not isinstance(call, ToolCall) or call.call_id in completed:
                        continue
                    unknown = (message.message_id, call.call_id) in intents
                    associated = {}
                    if call.name == "task":
                        children = [event.payload.get("child_session_id") for event in events
                                    if event.kind == "child_created" and event.payload.get("call_id") == call.call_id]
                        if not children:
                            try:
                                children = [item["session_id"] for item in cls.list_sessions(workspace, include_children=True)
                                            if item.get("parent_session_id") == session_id and item.get("parent_call_id") == call.call_id]
                            except StorageError:
                                store.warnings.append("部分子会话关联无法读取，请检查其独立记录。")
                        if children:
                            associated["child_session_id"] = children[-1]
                    result = ToolResult(call.call_id, ToolStatus.ERROR if unknown else ToolStatus.CANCELLED,
                                        "执行状态不确定；请检查实际文件或进程，不自动重放。" if unknown else "执行开始前中断；不自动重放。",
                                        {"tool_name": call.name, "recovery": True, "execution_unknown": unknown, **associated})
                    store.append_message(Message(MessageRole.TOOL, [result], origin={'kind':'recovery'}))
                    store.event("recovery", {"assistant_message_id": message.message_id, "call_id": call.call_id,
                                             "execution_unknown": unknown})
                    completed.add(call.call_id)
            validate_history(store.state.messages)
            store.save_metadata()
            return store
        except (OSError, ValueError, TypeError, StorageError) as exc:
            store.close()
            if isinstance(exc, StorageError):
                raise
            raise StorageError("会话记录损坏或版本不受支持，拒绝恢复。") from exc

    def _metadata(self):
        data = json.loads(self._path("session.json").read_text(encoding="utf-8"))
        envelope(data, {"session_id", "identity", "workspace", "status", "model", "created_at", "message_count"},
                 {"parent_session_id", "parent_call_id", "mode", "team_id", "lead_session_id"})
        if data["session_id"] != self.path.name or data["workspace"] != str(self.workspace):
            raise StorageError("会话 workspace 或身份不匹配。")
        SessionStatus(data["status"])
        timestamp(data["created_at"])
        integer(data["message_count"], "message_count")
        identity = AgentIdentity.from_dict(data["identity"])
        if identity.role.value == "subagent":
            local_id(data.get("parent_session_id"), "session")
            if not identity.parent_agent_id or type(data.get("parent_call_id")) is not str or not data["parent_call_id"].strip():
                raise StorageError("子会话父关联无效。")
            if data.get("mode") not in {"analyze", "edit"}:
                raise StorageError("子会话模式无效。")
        if identity.role.value == "teammate":
            local_id(data.get("team_id"), "team")
            local_id(data.get("lead_session_id"), "session")
        if type(data["model"]) is not str or not data["model"]:
            raise StorageError("会话模型记录无效。")
        return data

    @classmethod
    def list_sessions(cls, workspace, *, include_children=False):
        root = sessions_root(workspace)
        if not root.exists():
            return []
        result = []
        try:
            for path in sorted(root.iterdir()):
                local_id(path.name, "session")
                store = cls(workspace, path.name)
                metadata = store._metadata()
                if include_children or metadata["identity"]["role"] == "lead":
                    result.append(metadata)
        except (OSError, ValueError, TypeError) as exc:
            raise StorageError("无法读取会话列表，存在损坏或不受支持的数据。") from exc
        return result

    def close(self):
        if self.lock is not None:
            self.lock.close()
            self.lock = None


def list_sessions(workspace, *, include_children=False):
    return SessionStore.list_sessions(workspace, include_children=include_children)
