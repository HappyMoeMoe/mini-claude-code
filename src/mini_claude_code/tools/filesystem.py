"""UTF-8 file tools and bounded glob results under the authorized root."""

import fnmatch
import heapq
import itertools
import os
import stat
import tempfile
from pathlib import Path, PurePosixPath

from ..core.messages import ToolSpec
from ..core.files import open_regular_read
from ..core.session import ExecutionContext
from ..permissions.policy import PathPolicy
from .registry import ToolOutput, ToolRegistry


def _read_text(path: Path) -> str:
    with open_regular_read(path) as stream:
        content = stream.read()
    if "\0" in content:
        raise ValueError("Binary files are unsupported")
    return content


def _atomic_write(path: Path, content: str) -> int:
    payload = content.encode("utf-8")
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else None
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".mini-cc-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            if mode is not None:
                os.fchmod(stream.fileno(), mode)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return len(payload)


class FileTools:
    def __init__(self, paths: PathPolicy) -> None:
        self.paths = paths

    def read(self, args: dict, context: ExecutionContext) -> ToolOutput:
        path = self.paths.resolve(args["path"], context)
        offset, limit = args["offset"], args["limit"]
        with open_regular_read(path) as stream:
            lines = list(itertools.islice(stream, offset, offset + limit + 1))
        if any("\0" in line for line in lines):
            raise ValueError("Binary files are unsupported")
        more = len(lines) > limit
        body = "".join(f"{number}: {line.rstrip(chr(10) + chr(13))}\n"
                       for number, line in enumerate(lines[:limit], start=offset + 1))
        return ToolOutput(body + ("[more lines available]" if more else "[end of file]"),
                          metadata={"has_more": more, "offset": offset})

    def write(self, args: dict, context: ExecutionContext) -> ToolOutput:
        path = self.paths.resolve(args["path"], context)
        size = _atomic_write(path, args["content"])
        return ToolOutput(f"Wrote {path.relative_to(self.paths.effective_root or self.paths.root)} ({size} UTF-8 bytes)",
                          metadata={"bytes_written": size})

    def edit(self, args: dict, context: ExecutionContext) -> ToolOutput:
        path = self.paths.resolve(args["path"], context)
        content = _read_text(path)
        old, new = args["old_text"], args["new_text"]
        if content.count(old) != 1:
            raise ValueError("old_text must match exactly once")
        size = _atomic_write(path, content.replace(old, new, 1))
        return ToolOutput(f"Edited {path.relative_to(self.paths.effective_root or self.paths.root)} ({size} UTF-8 bytes)",
                          metadata={"bytes_written": size})

    def glob(self, args: dict, context: ExecutionContext) -> ToolOutput:
        self.paths.validate_pattern(args["pattern"])
        root = self.paths.cwd(context)
        parts = PurePosixPath(args["pattern"]).parts
        visited = set()

        def walk(directory: Path, index: int):
            if index == len(parts) or (directory, index) in visited:
                return
            visited.add((directory, index))
            pattern = parts[index]
            if pattern == "**":
                yield from walk(directory, index + 1)
            with os.scandir(directory) as entries:
                for entry in entries:
                    if entry.name in self.paths.PROTECTED:
                        continue
                    if pattern != "**" and not fnmatch.fnmatchcase(entry.name, pattern):
                        continue
                    candidate = Path(entry.path)
                    try:
                        self.paths.resolve(str(candidate), context)
                    except PermissionError:
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        if pattern == "**":
                            yield from walk(candidate, index)
                        elif index + 1 < len(parts):
                            yield from walk(candidate, index + 1)
                    elif entry.is_file() and (index == len(parts) - 1):
                        yield candidate.relative_to(root).as_posix()

        names = heapq.nsmallest(201, walk(root, 0))
        more = len(names) > 200
        return ToolOutput("\n".join(names[:200]) + ("\n[truncated at 200 files]" if more else ""),
                          metadata={"truncated": more, "count": min(200, len(names))})


def register_files(registry: ToolRegistry, paths: PathPolicy) -> None:
    handlers = FileTools(paths)
    path = {"type": "string", "minLength": 1}
    definitions = [
        ("read_file", "Read UTF-8 lines with real line numbers; offset is zero-based.",
         {"path": path, "offset": {"type": "integer", "minimum": 0, "default": 0},
          "limit": {"type": "integer", "minimum": 1, "maximum": 2000, "default": 200}}, ["path"], handlers.read),
        ("write_file", "Create or replace a UTF-8 file atomically.",
         {"path": path, "content": {"type": "string"}}, ["path", "content"], handlers.write),
        ("edit_file", "Replace one exact nonempty match; preserve untouched newlines.",
         {"path": path, "old_text": {"type": "string", "minLength": 1},
          "new_text": {"type": "string"}}, ["path", "old_text", "new_text"], handlers.edit),
        ("glob", "Find sorted workspace-relative files, supporting **; at most 200 results.",
         {"pattern": {"type": "string", "minLength": 1}}, ["pattern"], handlers.glob),
    ]
    for name, description, properties, required, handler in definitions:
        registry.register(ToolSpec(name, description, {"type": "object", "properties": properties,
                                                      "required": required, "additionalProperties": False}), handler)
