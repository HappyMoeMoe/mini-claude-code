"""Small tool table with one schema for display, defaults and validation."""

from copy import deepcopy
from dataclasses import dataclass
from typing import Callable

from ..core._validation import json_object
from ..core.messages import ToolSpec, ToolStatus
from ..core.session import ExecutionContext


@dataclass
class ToolOutput:
    output: str
    status: ToolStatus = ToolStatus.SUCCESS
    metadata: dict | None = None


Handler = Callable[[dict, ExecutionContext], ToolOutput]


@dataclass(frozen=True)
class RegisteredTool:
    spec: ToolSpec
    handler: Handler
    binding: object = None
    validator: object = None

    def validate(self, arguments: dict, *, cancellation=None) -> dict:
        json_object(arguments, "arguments")
        if self.validator is not None:return self.validator(arguments,cancellation=cancellation)
        schema = self.spec.input_schema
        properties = schema["properties"]
        if arguments.keys() - properties.keys() or not set(schema.get("required", [])) <= arguments.keys():
            raise ValueError("工具参数缺少必填字段或包含未知字段。")
        result = deepcopy(arguments)
        for name, rule in properties.items():
            if name not in result:
                if "default" not in rule:
                    continue
                result[name] = deepcopy(rule["default"])
            validate_value(result[name], rule, name)
        return result


def validate_value(value, rule, name):
    expected = {"string": str, "integer": int, "boolean": bool, "array": list, "object": dict}[rule["type"]]
    if type(value) is not expected:
        raise ValueError(f"参数 {name} 类型错误。")
    if "enum" in rule and value not in rule["enum"]:
        raise ValueError(f"参数 {name} 不在允许的枚举中。")
    if expected in {str, list}:
        minimum = rule.get("minLength" if expected is str else "minItems", 0)
        maximum = rule.get("maxLength" if expected is str else "maxItems", len(value))
        if not minimum <= len(value) <= maximum:
            raise ValueError(f"参数 {name} 长度超出范围。")
    if expected is int and not rule.get("minimum", value) <= value <= rule.get("maximum", value):
        raise ValueError(f"参数 {name} 超出允许范围。")
    if expected is list:
        for index, item in enumerate(value):
            validate_value(item, rule["items"], f"{name}[{index}]")
    if expected is dict:
        properties = rule.get("properties", {})
        if not set(rule.get("required", [])) <= value.keys():
            raise ValueError(f"参数 {name} 缺少字段。")
        if rule.get("additionalProperties") is False and value.keys() - properties.keys():
            raise ValueError(f"参数 {name} 包含未知字段。")
        for key, item in value.items():
            if key in properties:
                validate_value(item, properties[key], f"{name}.{key}")


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool] = {}
        self.version = 0

    def register(self, spec: ToolSpec, handler: Handler, *, binding=None, validator=None) -> None:
        if spec.name in self._tools or not callable(handler):
            raise ValueError("工具重名或缺少可执行 handler。")
        self._tools[spec.name] = RegisteredTool(deepcopy(spec), handler, binding, validator)
        self.version += 1

    def remove(self,names):
        changed=False
        for name in names:
            if name in self._tools:del self._tools[name];changed=True
        if changed:self.version+=1

    def snapshot(self,names):
        return self.version,{name:self._tools[name] for name in names if name in self._tools}

    def get(self, name: str) -> RegisteredTool | None:
        return self._tools.get(name)

    def specs(self) -> list[ToolSpec]:
        return [deepcopy(tool.spec) for tool in self._tools.values()]

    def retain(self, names):
        """Host-only removal of every ungranted handler and schema."""
        allowed = frozenset(names)
        self.remove(set(self._tools)-allowed)
