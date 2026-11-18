"""The main thread exclusively owns normal input, approvals and display."""

import sys
import threading
import os
import selectors
import codecs
import stat
from collections import deque

from .core.redaction import redact_arguments


class ConsoleBroker:
    def __init__(self):
        self.interactive = sys.stdin.isatty()
        self.streaming = False
        self.lines = deque()
        self.buffer = ''
        self.decoder = codecs.getincrementaldecoder('utf-8')()
        self.eof = False
        self.prompted = False

    def _newline(self):
        if self.streaming:
            print(flush=True)
            self.streaming = False

    def _fd(self):
        try:return sys.stdin.fileno()
        except (ValueError,OSError,AttributeError):return None

    def poll(self):
        if threading.current_thread() is not threading.main_thread():raise RuntimeError('Only the main thread may read input')
        if self.lines:return self.lines.popleft()
        if self.eof:
            if self.buffer:
                line,self.buffer=self.buffer,'';return line
            raise EOFError()
        fd=self._fd()
        if fd is None:return None
        return self._wait(fd,None,0)

    def _wait(self,fd,wakeup,timeout):
        with selectors.DefaultSelector() as selector:
            try:selector.register(fd,selectors.EVENT_READ,'input')
            except (PermissionError,OSError):
                regular = stat.S_ISREG(os.fstat(fd).st_mode)
                return input('mini-cc> ' if self.interactive else '') if timeout is None or regular else None
            if wakeup is not None:selector.register(wakeup.read_fd,selectors.EVENT_READ,'wake')
            ready=selector.select(timeout)
            # Completed human input takes precedence over wake signals.
            if any(key.data=='input' for key,_ in ready):
                raw=os.read(fd,65536)
                if not raw:self.eof=True
                self.buffer+=self.decoder.decode(raw,final=not raw)
                while '\n' in self.buffer:
                    line,self.buffer=self.buffer.split('\n',1);self.lines.append(line.rstrip('\r'))
                if self.lines:
                    self.prompted=False;return self.lines.popleft()
                if self.eof:
                    if self.buffer:line,self.buffer=self.buffer,'';return line
                    raise EOFError()
            if wakeup is not None and any(key.data=='wake' for key,_ in ready):
                wakeup.drain();return None
        return None

    def read(self, wakeup=None):
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError("Only the main thread may read the terminal")
        self._newline()
        if self.lines:
            self.prompted=False
            return self.lines.popleft()
        if self.eof:
            if self.buffer:
                line,self.buffer=self.buffer,''
                self.prompted=False
                return line
            raise EOFError()
        fd=self._fd()
        if fd is not None:
            if self.interactive and not self.prompted:
                print('mini-cc> ',end='',flush=True);self.prompted=True
            # poll() may already hold partial bytes or complete lines. Keep
            # using its decoder/buffer rather than switching to input().
            while True:
                line=self._wait(fd,wakeup,None)
                if line is not None or wakeup is not None:return line
        return input("mini-cc> " if self.interactive else "")

    def approve(self, call, arguments, context):
        if threading.current_thread() is not threading.main_thread():raise RuntimeError('Only the main thread may approve commands')
        if not self.interactive or not context.interactive:
            return False
        self._newline()
        print(f"\nShell 审批\ncwd: {context.cwd}\ntimeout: {arguments['timeout_seconds']} 秒")
        print(f"command:\n{arguments['command']}")
        print('mode: '+('background' if arguments.get('run_in_background') else 'foreground'))
        try:
            return input("允许本次命令？仅输入 y 批准 [y/N]: ").strip() == "y"
        except EOFError:
            return False

    def approve_mcp(self,binding,call,arguments,context):
        import json
        if threading.current_thread() is not threading.main_thread():raise RuntimeError('Only the main thread may approve MCP')
        if not self.interactive or not context.interactive:return False
        self._newline()
        text=json.dumps(arguments,ensure_ascii=False,indent=2)
        print(f"\nMCP 审批\nServer: {binding['server_name']}\n工具原名: {binding['raw_tool_name']}\neffect: {binding['effect']}\ncwd: {context.cwd}")
        print('参数（敏感字段已遮蔽）：\n'+text[:1600])
        try:
            answer=input('仅 y 批准本次；v 查看完整脱敏参数；其他拒绝 [y/v/N]: ').strip()
            if answer=='v':
                print(text)
                answer=input('允许本次调用？仅 y 批准 [y/N]: ').strip()
            return answer=='y'
        except EOFError:return False

    def display(self, kind, value):
        if kind == "delta":
            print(value, end="", flush=True)
            self.streaming = True
            return
        self._newline()
        if self.prompted:
            print(flush=True);self.prompted=False
        if kind in {"text", "notice"}:
            print(value, flush=True)
        elif kind == "call":
            summary = ", ".join(f"{key}=<{len(val)} chars>" if key in {"content", "old_text", "new_text"}
                                and isinstance(val, str) else f"{key}={val!r}"
                                for key, val in redact_arguments(value.arguments).items())
            print(f"> {value.name} {summary[:240]}", flush=True)
        elif kind == "result":
            details = f' timezone={value.metadata["timezone"]}' if value.metadata.get('timezone') else ''
            print(f"[{value.status.value}]{details} {value.output[:500]}", flush=True)
