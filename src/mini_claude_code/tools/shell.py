"""Synchronous Bash with temporary output capture and owned process groups."""

import os
import shutil
import selectors
import signal
import subprocess
import tempfile
import time

from ..core.errors import ConfigurationError, StorageError
from ..core.cancellation import Cancelled
from ..core.messages import ToolSpec, ToolStatus
from ..core.session import ExecutionContext
from .registry import ToolOutput, ToolRegistry


class ShellRunner:
    def __init__(self) -> None:
        self.bash = shutil.which("bash")
        if self.bash is None:
            raise ConfigurationError("Bash is required for chat on Linux")
        self._active: set[subprocess.Popen] = set()
        self.cancellation = None
        self.output_sink = None
        self.capture_check = None
        self.invocation = None
        self.unconfirmed_work = False
        self.background_handler = None
        self.on_started = None
        self.capture_closed = None

    @staticmethod
    def _stop(process: subprocess.Popen) -> None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            process.wait()
            return
        deadline = time.monotonic() + 0.3
        while time.monotonic() < deadline:
            process.poll()
            try:
                os.killpg(process.pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.02)
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()

    def _stop_owned(self, process):
        try:
            self._stop(process)
            if process.poll() is None:
                raise RuntimeError('Shell process termination was not confirmed')
        except BaseException as exc:
            self.unconfirmed_work = True
            raise StorageError('Shell 进程未确认停止；保留进程追踪和任务认领，停止后续操作。') from exc

    def _cleanup(self, process, selector=None):
        error = None
        if process is not None:
            try:self._stop_owned(process)
            except BaseException as exc:error = exc
            for pipe in (process.stdout, process.stderr):
                if pipe is not None:
                    try:pipe.close()
                    except BaseException as exc:error = error or exc
        if selector is not None:
            try:selector.close()
            except BaseException as exc:error = error or exc
        if error is not None:
            self.unconfirmed_work = True
            raise StorageError('Shell 资源清理未确认完成；保留追踪和任务认领，停止后续操作。') from error
        if process is not None:self._active.discard(process)

    def close(self) -> None:
        error = None
        for process in list(self._active):
            try:self._cleanup(process)
            except BaseException as exc:error = error or exc
        if error is not None:
            raise StorageError('Shell 资源未确认全部停止；保留任务认领。') from error

    def set_invocation(self, call, assistant_message_id):
        self.invocation = (call, assistant_message_id) if call is not None else None

    def run(self, args: dict, context: ExecutionContext) -> ToolOutput:
        if args.get('run_in_background',False):
            if self.background_handler is None:raise ValueError('该执行者不能启动后台 Shell。')
            return self.background_handler(args,context,self.invocation)
        try:return self._run(args,context)
        finally:
            if self.capture_closed is not None:self.capture_closed()

    def _run(self, args: dict, context: ExecutionContext) -> ToolOutput:
        from ..storage.artifacts import SINGLE_LIMIT
        process = None
        timed_out = cancelled = truncated = False
        totals = {"stdout": 0, "stderr": 0}
        captured = 0
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            selector = selectors.DefaultSelector()
            try:
                env = os.environ.copy()
                env.pop("BASH_ENV", None); env.pop("ENV", None)
                process = subprocess.Popen(
                    [self.bash, "--noprofile", "--norc", "-c", args["command"]],
                    cwd=context.cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, start_new_session=True, env=env)
                self._active.add(process)
                if self.on_started is not None:self.on_started(process)
                selector.register(process.stdout, selectors.EVENT_READ, ("stdout", stdout))
                selector.register(process.stderr, selectors.EVENT_READ, ("stderr", stderr))
                deadline = time.monotonic() + args["timeout_seconds"]
                stopped_at = None
                while selector.get_map() or process.poll() is None:
                    if self.cancellation is not None: self.cancellation.check()
                    now = time.monotonic()
                    if stopped_at is None and (process.poll() is not None or now >= deadline):
                        timed_out = process.poll() is None
                        self._stop_owned(process)
                        stopped_at = time.monotonic()
                    if stopped_at is not None and now - stopped_at > 0.5:
                        if selector.get_map():
                            self.unconfirmed_work = True
                        truncated = bool(selector.get_map()) or truncated
                        break
                    for key, _ in selector.select(0.05):
                        chunk = os.read(key.fileobj.fileno(), 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        channel, target = key.data
                        totals[channel] += len(chunk)
                        keep = chunk[:max(0, SINGLE_LIMIT - captured)]
                        if keep:
                            if self.capture_check is not None: self.capture_check(captured + len(keep))
                            target.write(keep)
                            captured += len(keep)
                        if len(keep) != len(chunk): truncated = True
            except (KeyboardInterrupt, Cancelled):
                if self.cancellation is None: raise
                self.cancellation.cancel()
                cancelled = True
                truncated = True
            finally:
                self._cleanup(process,selector)
            stdout.seek(0); stderr.seek(0)
            out, err = stdout.read(SINGLE_LIMIT), stderr.read(SINGLE_LIMIT)
            output = (f"stdout:\n{out.decode('utf-8', errors='replace')}\n"
                      f"stderr:\n{err.decode('utf-8', errors='replace')}\n"
                      f"exit_code: {process.returncode}; timed_out: {timed_out}; cancelled: {cancelled}")
            metadata = {"exit_code": process.returncode, "timed_out": timed_out,
                        "stdout_bytes_total": totals['stdout'], "stderr_bytes_total": totals['stderr'],
                        "truncated_at_capture": truncated, "execution_unknown": cancelled,
                        "resources_unconfirmed": self.unconfirmed_work}
            metadata['cancelled'] = cancelled
            if truncated:
                output += "\n[truncated_at_capture: 超额或未完成捕获的输出不可恢复]"
                metadata['bytes_total'] = None
            if self.unconfirmed_work:
                output += "\n[所属输出管道未关闭，无法确认全部工作已停止；请检查派生进程。]"
            result = ToolOutput(output, ToolStatus.ERROR if timed_out or cancelled or self.unconfirmed_work or process.returncode != 0 else ToolStatus.SUCCESS, metadata)
            # Persist before temporary captures close; count both captures and new artifact in quota.
            if self.output_sink is not None:
                return self.output_sink(result, context, self.invocation, captured)
            return result


def register_shell(registry: ToolRegistry, shell: ShellRunner, *, allow_background=True, expose_background=True) -> None:
    registry.register(ToolSpec("bash", "Run one Bash command after single-call approval." + (" Explicit background mode returns launch status only; final output arrives as a separate event. Do not poll or restart it." if expose_background else ""), {
        "type": "object", "properties": {
            "command": {"type": "string", "minLength": 1},
            "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 600, "default": 120},
            **({"run_in_background": {"type":"boolean","default":False, **({} if allow_background else {'enum':[False]})}} if expose_background else {}),
        }, "required": ["command"], "additionalProperties": False,
    }), shell.run)
