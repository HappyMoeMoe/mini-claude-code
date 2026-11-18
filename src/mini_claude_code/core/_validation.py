"""Small shared validators for explicit JSON codecs, not a schema framework."""

import math
import re
from datetime import datetime, timezone
from enum import Enum
from uuid import uuid4

from .errors import ContractError

SCHEMA_VERSION = 1


def new_id(kind: str) -> str:
    if kind not in {"session", "agent", "task", "event", "message", "turn", "artifact", "context", "memory", "claim",
                    "background", "job", "occurrence", "attempt", "team", "request", "operation"}:
        raise ContractError("Unsupported local ID kind")
    return f"{kind}_{uuid4().hex}"


def local_id(value: object, kind: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(rf"{kind}_[0-9a-f]{{32}}", value):
        raise ContractError(f"Expected {kind}_ followed by 32 lowercase UUID hex digits")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def timestamp(value: object) -> None:
    text(value, "created_at")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ContractError("created_at must be a UTC ISO 8601 timestamp") from exc
    if "T" not in value or parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
        raise ContractError("created_at must be a UTC ISO 8601 timestamp")


def text(value: object, name: str, *, empty: bool = False) -> None:
    if type(value) is not str or (not empty and not value.strip()):
        raise ContractError(f"{name} must be {'a string' if empty else 'a nonempty string'}")


def integer(value: object, name: str, *, minimum: int = 0) -> None:
    if type(value) is not int or value < minimum:
        raise ContractError(f"{name} must be an integer >= {minimum}")


def enum_value(value: object, enum: type[Enum], name: str):
    if not isinstance(value, (str, enum)):
        raise ContractError(f"Invalid {name}")
    try:
        return enum(value)
    except ValueError as exc:
        raise ContractError(f"Invalid {name}") from exc


def json_value(value: object) -> None:
    """Reject coercible Python values as well as NaN and infinity."""
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) is list:
        for item in value:
            json_value(item)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for item in value.values():
            json_value(item)
        return
    raise ContractError("Expected plain JSON values with finite numbers and string object keys")


def json_object(value: object, name: str) -> None:
    if type(value) is not dict:
        raise ContractError(f"{name} must be a JSON object")
    try:
        json_value(value)
    except RecursionError as exc:
        raise ContractError(f"{name} cannot contain circular or excessively nested values") from exc


def fields(data: object, required: set[str], optional: set[str] | None = None) -> dict:
    if type(data) is not dict:
        raise ContractError("Expected a JSON object")
    if not required <= data.keys() or data.keys() - required - (optional or set()):
        raise ContractError("Missing required fields or unrecognized fields")
    return data


def envelope(data: object, required: set[str], optional: set[str] | None = None) -> dict:
    result = fields(data, required | {"schema_version"}, optional)
    if type(result["schema_version"]) is not int or result["schema_version"] != SCHEMA_VERSION:
        raise ContractError("Unsupported schema_version; expected 1")
    return result


def array(value: object, name: str) -> list:
    if type(value) is not list:
        raise ContractError(f"{name} must be an array")
    return value

