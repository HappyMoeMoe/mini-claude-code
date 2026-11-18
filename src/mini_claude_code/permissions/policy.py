"""Application-level file boundaries and single-call Shell decisions."""

from enum import Enum
from pathlib import Path, PurePosixPath

from ..core.session import ExecutionContext


class PathPolicy:
    PROTECTED = {".git", ".mini-claude-code"}

    def __init__(self, root: Path) -> None:
        self.root = root.resolve(strict=True)
        self.effective_root = None

    def cwd(self, context: ExecutionContext) -> Path:
        if self.effective_root is not None:
            root = Path(self.effective_root).resolve(strict=True)
            cwd = Path(context.cwd).resolve(strict=True)
            if cwd != root or not root.is_dir(): raise PermissionError("Assignment cwd changed")
            return cwd
        cwd = Path(context.cwd).resolve(strict=True)
        if not cwd.is_dir() or not cwd.is_relative_to(self.root):
            raise PermissionError("Execution cwd is outside the authorized workspace")
        if self.PROTECTED.intersection(cwd.relative_to(self.root).parts):
            raise PermissionError("Execution cwd is a protected management directory")
        return cwd

    def resolve(self, value: str, context: ExecutionContext) -> Path:
        cwd = self.cwd(context)
        candidate = Path(value)
        candidate = candidate if candidate.is_absolute() else cwd / candidate
        # Check both lexical aliases and resolved targets (including missing parents).
        lexical = candidate.relative_to(self.effective_root) if self.effective_root is not None and candidate.is_relative_to(self.effective_root) else candidate
        if self.PROTECTED.intersection(lexical.parts):
            raise PermissionError("Direct access to management directories is denied")
        target = candidate.resolve()
        root = Path(self.effective_root) if self.effective_root is not None else self.root
        if not target.is_relative_to(root):
            raise PermissionError("File path is outside the authorized workspace")
        if self.PROTECTED.intersection(target.relative_to(root).parts):
            raise PermissionError("Direct access to management directories is denied")
        return target

    @staticmethod
    def validate_pattern(pattern: str) -> None:
        if PurePosixPath(pattern).is_absolute() or ".." in PurePosixPath(pattern).parts:
            raise PermissionError("Glob must be relative and cannot contain ..")


class Decision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


class PermissionPolicy:
    def decide(self, name: str, context: ExecutionContext) -> Decision:
        if name == "bash":
            return Decision.ASK
        if name in {"read_file", "write_file", "edit_file", "glob", "todo_write", "load_skill", "task", "read_tool_output", "compact",
                    "create_task", "update_task", "list_tasks", "get_task", "claim_task", "complete_task",
                    "schedule_cron", "list_crons", "cancel_cron", "spawn_teammate", "list_teammates", "send_message",
                    "request_plan", "submit_plan", "review_plan", "request_shutdown", "create_worktree", "connect_mcp"}:
            return Decision.ALLOW
        return Decision.DENY


class RestrictedPolicy:
    """A child's grants can only shrink the parent's actual policy."""
    def __init__(self, parent, names):
        self.parent, self.names = parent, frozenset(names)

    def permits_tool(self, name):
        return name in self.names

    def decide(self, name, context):
        return self.parent.decide(name, context) if name in self.names else Decision.DENY
