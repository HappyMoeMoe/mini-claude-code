"""A session's plan is reconstructed solely from committed successful results."""

from dataclasses import dataclass
import json

from ..core.messages import ToolResult, ToolSpec, ToolStatus
from ..core.errors import StorageError
from ..tools.registry import ToolOutput


@dataclass(frozen=True)
class TodoItem:
    content: str
    status: str

    def to_dict(self):
        return {"content": self.content, "status": self.status}


def normalize(todos):
    if type(todos) is not list or len(todos) > 30:
        raise ValueError("todos 必须是最多 30 项的数组。")
    items = []
    for value in todos:
        if type(value) is not dict or set(value) != {"content", "status"}:
            raise ValueError("Todo 必须包含且仅包含 content 和 status。")
        content, status = value["content"], value["status"]
        if type(content) is not str or not content.strip() or len(content.strip()) > 500:
            raise ValueError("Todo content 去除首尾空白后须为 1–500 字符。")
        if type(status) is not str or status not in {"pending", "in_progress", "completed"}:
            raise ValueError("Todo status 无效。")
        items.append(TodoItem(content.strip(), status))
    if sum(item.status == "in_progress" for item in items) > 1:
        raise ValueError("同时最多一项 Todo 为 in_progress。")
    return items


class TodoManager:
    def __init__(self, state):
        self.state = state

    def current(self):
        for message in reversed(self.state.messages):
            for block in reversed(message.content):
                if (isinstance(block, ToolResult) and block.status == ToolStatus.SUCCESS
                        and block.metadata.get("tool_name") == "todo_write"
                        and not block.metadata.get('execution_unknown')):
                    try:
                        return normalize(block.metadata["todos"])
                    except (KeyError, ValueError) as exc:
                        raise StorageError("已提交的 Todo 记录无效，不能恢复会话计划。") from exc
        return []

    def write(self, args, context):
        todos = [item.to_dict() for item in normalize(args["todos"])]
        return ToolOutput(json.dumps({"todos": todos, "count": len(todos)}, ensure_ascii=False),
                          metadata={"todos": todos, "count": len(todos)})

    def register(self, registry):
        registry.register(ToolSpec("todo_write", "Replace this session's entire Todo plan. At most one item in_progress.", {
            "type": "object", "properties": {"todos": {"type": "array", "maxItems": 30,
                "items": {"type": "object", "properties": {
                    "content": {"type": "string", "minLength": 1},
                    "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}},
                    "required": ["content", "status"], "additionalProperties": False}}},
            "required": ["todos"], "additionalProperties": False}), self.write)
