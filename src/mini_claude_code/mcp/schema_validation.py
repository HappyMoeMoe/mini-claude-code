"""Disposable JSON-only workers bound schema CPU work without weakening rules."""

from dataclasses import dataclass
import json
import subprocess
import sys
import time

from .config import MCPError

VALIDATION_TIMEOUT = 2.0
MAX_INPUT = 16 * 1024 * 1024 + 32 * 1024 + 4096
WORKER_MEMORY = 256 * 1024 * 1024


class SchemaValidationTimeout(MCPError):
    pass


class DiscoveryTimeout(MCPError):
    pass


def check_deadline(deadline):
    if deadline is not None and time.monotonic() >= deadline:
        raise DiscoveryTimeout('tools/list 总发现超时。')


def _run(schema, *, operation, arguments=None, cancellation=None, deadline=None):
    validation_deadline = time.monotonic() + VALIDATION_TIMEOUT
    if cancellation is not None:
        cancellation.check()
    check_deadline(deadline)
    payload = json.dumps({'schema': schema, 'operation': operation, 'arguments': arguments},
                         ensure_ascii=False, allow_nan=False).encode('utf-8')
    if len(payload) > MAX_INPUT:
        raise MCPError('JSON Schema 校验输入超过大小上限。')
    process = None
    try:
        check_deadline(deadline)
        # Isolated mode excludes workspace/PYTHONPATH modules from worker imports.
        process = subprocess.Popen([sys.executable, '-I', '-m', __name__], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   start_new_session=True)
        first = True
        while True:
            if cancellation is not None:
                cancellation.check()
            check_deadline(deadline)
            remaining = min(validation_deadline, deadline if deadline is not None else validation_deadline) - time.monotonic()
            if remaining <= 0:
                check_deadline(deadline)
                raise SchemaValidationTimeout('本地 JSON Schema 校验超时；未忽略约束。')
            try:
                output, _ = process.communicate(payload if first else None, timeout=min(.05, remaining))
                break
            except subprocess.TimeoutExpired:
                first = False
        if cancellation is not None:
            cancellation.check()
        check_deadline(deadline)
        if time.monotonic() >= validation_deadline:
            raise SchemaValidationTimeout('本地 JSON Schema 校验超时；未忽略约束。')
        if process.returncode != 0 or output != b'valid\n':
            raise MCPError('JSON Schema 校验失败或达到资源上限；未削弱或忽略约束。')
    except OSError as exc:
        raise MCPError('无法启动本地 JSON Schema 校验进程；未跳过校验。') from exc
    finally:
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.communicate()  # Reap on success, timeout, token cancellation and SIGINT.


@dataclass(frozen=True)
class SchemaValidator:
    schema: dict

    def validate(self, arguments, *, cancellation=None):
        _run(self.schema, operation='validate', arguments=arguments, cancellation=cancellation)


def check_schema(schema, *, cancellation=None, deadline=None):
    _run(schema, operation='check', cancellation=cancellation, deadline=deadline)


def _worker():
    # Limits apply only to this fresh interpreter, never to Runtime/teammate threads.
    import resource
    resource.setrlimit(resource.RLIMIT_AS, (WORKER_MEMORY, WORKER_MEMORY))
    from jsonschema import Draft202012Validator, Draft7Validator
    from referencing import Registry
    from referencing.exceptions import NoSuchResource

    try:
        raw = sys.stdin.buffer.read(MAX_INPUT + 1)
        if len(raw) > MAX_INPUT:
            return 1
        data = json.loads(raw)
        schema = data['schema']
        dialect = schema.get('$schema', 'https://json-schema.org/draft/2020-12/schema')
        cls = {'https://json-schema.org/draft/2020-12/schema': Draft202012Validator,
               'http://json-schema.org/draft-07/schema#': Draft7Validator,
               'https://json-schema.org/draft-07/schema#': Draft7Validator}[dialect]
        if data['operation'] == 'check':
            cls.check_schema(schema)
        elif data['operation'] == 'validate':
            def refuse(uri):
                raise NoSuchResource(ref=uri)
            cls(schema, registry=Registry(retrieve=refuse)).validate(data['arguments'])
        else:
            return 1
        sys.stdout.buffer.write(b'valid\n')
        return 0
    except Exception:
        # Never return exception strings, instance contents or schema annotations.
        return 1


if __name__ == '__main__':
    raise SystemExit(_worker())
