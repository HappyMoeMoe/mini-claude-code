"""Official SDK session over a bounded, process-group-owned stdio transport.

All SDK contexts enter/exit in their connection actor on one lazy I/O thread.
This layer never calls a model, reads terminal input, or writes a session journal.
"""
import asyncio
from contextlib import asynccontextmanager
import concurrent.futures
import os
import signal
import threading
import time

import anyio
from mcp import ClientSession,types
from mcp.shared.message import SessionMessage

from ..core.cancellation import Cancelled
from .adapter import MAX_MESSAGE
from .config import MCPError

BASELINE='2025-11-25'
CLOSE_GRACE_SECONDS=4.0
CLOSE_CANCEL_SECONDS=3.0
CLOSE_WAIT_SECONDS=8.0


class Capacity:
    def __init__(self):self.lock=threading.Lock();self.count=0
    def acquire(self):
        with self.lock:
            if self.count>=8:raise MCPError('Runtime MCP 活动连接已达 8 个上限。')
            self.count+=1
    def release(self):
        with self.lock:self.count-=1


class IOHub:
    def __init__(self):
        self.loop=None;self.thread=None;self.lock=threading.Lock();self.capacity=Capacity();self.closed=False
    def start(self):
        with self.lock:
            if self.closed:raise MCPError('MCP I/O 已关闭。')
            if self.thread is not None:return
            ready=threading.Event()
            def run():
                self.loop=asyncio.new_event_loop();asyncio.set_event_loop(self.loop);ready.set()
                try:self.loop.run_forever()
                finally:
                    pending=asyncio.all_tasks(self.loop)
                    for task in pending:task.cancel()
                    if pending:self.loop.run_until_complete(asyncio.gather(*pending,return_exceptions=True))
                    self.loop.run_until_complete(self.loop.shutdown_asyncgens());self.loop.close()
            self.thread=threading.Thread(target=run,name='mcp-io',daemon=True);self.thread.start()
            if not ready.wait(2):raise MCPError('MCP I/O 未能启动。')
    def submit(self,coro):
        self.start();return asyncio.run_coroutine_threadsafe(coro,self.loop)
    def close(self):
        with self.lock:
            if self.closed:return
            if self.capacity.count:raise MCPError('MCP 仍有未回收连接，不能停止 I/O loop。')
            self.closed=True
            if self.loop is not None:self.loop.call_soon_threadsafe(self.loop.stop)
        if self.thread is not None:
            self.thread.join(4)
            if self.thread.is_alive():raise MCPError('MCP I/O 线程未确认退出。')


class StdioConnection:
    def __init__(self,hub,config,cwd,wake=lambda:None):
        self.hub,self.config,self.cwd,self.wake=hub,config,cwd,wake
        self.state='disconnected';self.version=None;self.session=None;self.process=None
        self.dirty=threading.Event();self.error=None;self.stderr=bytearray();self.stderr_lock=threading.Lock()
        self.stop=None;self.actor=None;self.ready=None;self.slot=False;self.confirmed=True
        self.cleanup=None;self.transport_stopped=False
        self.trace=None;self.active=set();self.closed=False
    def _fail(self,category):
        self.error=category;self.state='failed';self.wake()
        if self.stop is not None:self.stop.set()
    async def _notice(self,message):
        if isinstance(message,types.ServerNotification) and isinstance(message.root,types.ToolListChangedNotification):
            self.dirty.set();self.wake()
        elif isinstance(message,Exception):self._fail('protocol_error')
    async def _read(self,writer):
        try:
            async with writer:
                while True:
                    line=await self.process.stdout.readuntil(b'\n')
                    if len(line)>MAX_MESSAGE:raise MCPError('message_too_large')
                    value=types.JSONRPCMessage.model_validate_json(line)
                    await writer.send(SessionMessage(value))
        except asyncio.CancelledError:raise
        except Exception:self._fail('stdout_closed_or_invalid')
    async def _write(self,reader):
        try:
            async with reader:
                async for value in reader:
                    wire=value.message.model_dump_json(by_alias=True,exclude_none=True).encode()+b'\n'
                    if len(wire)>MAX_MESSAGE:raise MCPError('message_too_large')
                    root=value.message.root
                    if isinstance(root,types.JSONRPCRequest) and root.method=='tools/call' and self.trace is not None:
                        self.trace.update(request_id=root.id,sent=True)
                    self.process.stdin.write(wire);await self.process.stdin.drain()
        except asyncio.CancelledError:raise
        except Exception:self._fail('stdin_closed_or_invalid')
    async def _stderr(self):
        try:
            while True:
                data=await self.process.stderr.read(8192)
                if not data:break
                with self.stderr_lock:
                    self.stderr.extend(data)
                    if len(self.stderr)>1024*1024:del self.stderr[:-1024*1024]
        except asyncio.CancelledError:raise
        except Exception:pass
    def _group_alive(self):
        if self.process is None:return False
        try:os.killpg(self.process.pid,0);return True
        except ProcessLookupError:return False
    async def _reap(self):
        if self.process is None:return True
        proc=self.process
        if proc.stdin is not None:proc.stdin.close()
        try:await asyncio.wait_for(proc.wait(),.5)
        except asyncio.TimeoutError:pass
        # Also stop descendants when the group leader has already exited.
        for sig,delay in ((signal.SIGTERM,.5),(signal.SIGKILL,1.5)):
            if not self._group_alive():break
            try:os.killpg(proc.pid,sig)
            except ProcessLookupError:break
            try:await asyncio.wait_for(proc.wait(),delay)
            except asyncio.TimeoutError:pass
            await asyncio.sleep(.02)
        stopped=proc.returncode is not None and not self._group_alive()
        if not stopped:self.error='process_group_unconfirmed'
        return stopped
    async def _cleanup_transport(self,tasks,streams):
        stopped=False;error=None
        try:stopped=await self._reap()
        except BaseException as exc:error=exc
        # Every transport resource gets a cleanup attempt even if reaping fails.
        for task in tasks:task.cancel()
        try:await asyncio.gather(*tasks,return_exceptions=True)
        except BaseException as exc:error=error or exc
        for stream in streams:
            try:await stream.aclose()
            except BaseException as exc:error=error or exc
        if error is not None:raise error
        return stopped
    @asynccontextmanager
    async def _transport(self):
        read_writer,read_stream=anyio.create_memory_object_stream(0)
        write_stream,write_reader=anyio.create_memory_object_stream(0)
        tasks=[]
        self.confirmed=False;self.transport_stopped=False
        try:
            self.process=await asyncio.create_subprocess_exec(self.config.command,*self.config.args,cwd=self.cwd,
                env=self.config.environment(),stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,start_new_session=True,limit=MAX_MESSAGE+1)
            tasks=[asyncio.create_task(self._read(read_writer)),asyncio.create_task(self._write(write_reader)),asyncio.create_task(self._stderr())]
            yield read_stream,write_stream
        finally:
            self.cleanup=asyncio.create_task(self._cleanup_transport(
                tasks,(read_writer,read_stream,write_stream,write_reader)))
            # Owner cancellation (including the close timeout) cannot interrupt
            # process/I/O cleanup. The caller still has a bounded close wait.
            while True:
                try:
                    self.transport_stopped=await asyncio.shield(self.cleanup)
                    break
                except asyncio.CancelledError:
                    if self.cleanup.done():
                        self.transport_stopped=self.cleanup.result()
                        break
    async def _actor(self):
        self.stop=asyncio.Event()
        try:
            async with self._transport() as streams:
                async with ClientSession(*streams,message_handler=self._notice) as session:
                    self.session=session
                    async with asyncio.timeout(self.config.timeouts['connect']):result=await session.initialize()
                    if result.protocolVersion!=BASELINE:raise MCPError('仅支持所声明的 MCP 2025-11-25 基线。')
                    if result.capabilities.tools is None:raise MCPError('Server 未声明 tools 能力。')
                    self.version=result.protocolVersion;self.state='ready'
                    self.ready.set_result(True)
                    await self.stop.wait()
                    pending=[t for t in self.active if t is not asyncio.current_task()]
                    for task in pending:task.cancel()
                    await asyncio.gather(*pending,return_exceptions=True)
        except BaseException as exc:
            self.error=self.error or type(exc).__name__;self.state='failed'
            if not self.ready.done():self.ready.set_exception(MCPError('MCP 初始化失败；详情类型 '+self.error+'。'))
        finally:
            self.session=None
            self.confirmed=bool(self.transport_stopped)
            if self.slot and self.confirmed:self.hub.capacity.release();self.slot=False
            self.wake()
    async def _open(self):
        if self.closed:raise MCPError('MCP 连接已关闭，不能启动进程。')
        self.ready=asyncio.get_running_loop().create_future();self.actor=asyncio.create_task(self._actor())
        await self.ready
    def _wait(self,future,seconds,token=None):
        deadline=time.monotonic()+seconds
        try:
            while True:
                if token is not None:token.check()
                try:return future.result(timeout=min(.05,max(.001,deadline-time.monotonic())))
                except concurrent.futures.TimeoutError:
                    if time.monotonic()>=deadline:raise MCPError('MCP 操作超时。')
        except (BaseException,):
            future.cancel();raise
    def open(self,token=None):
        if self.closed:raise MCPError('MCP 连接已关闭，不能重新使用。')
        if token is not None:token.check()
        self.hub.capacity.acquire();self.slot=True;self.confirmed=False;self.state='connecting'
        try:self._wait(self.hub.submit(self._open()),self.config.timeouts['connect']+1,token)
        except BaseException:
            self.close();raise
    async def _operation(self,kind,arguments,trace):
        task=asyncio.current_task();self.active.add(task)
        try:
            if self.state!='ready' or self.session is None:raise MCPError('MCP 连接不再 ready。')
            self.trace=trace
            if kind=='list':
                result=await self.session.list_tools(params=types.PaginatedRequestParams(cursor=arguments))
            else:
                # Public SDK request API: avoid call_tool's implicit output-schema/list fetch.
                result=await self.session.send_request(types.ClientRequest(types.CallToolRequest(
                    params=types.CallToolRequestParams(name=arguments[0],arguments=arguments[1]))),types.CallToolResult)
            return result.model_dump(mode='json',by_alias=True,exclude_none=True)
        except asyncio.CancelledError:
            if trace.get('sent') and trace.get('request_id') is not None and self.session is not None:
                try:
                    async with asyncio.timeout(.2):
                        await self.session.send_notification(types.ClientNotification(types.CancelledNotification(
                            params=types.CancelledNotificationParams(requestId=trace['request_id'],reason='Host cancelled or timed out'))))
                except BaseException:pass
            raise
        finally:self.trace=None;self.active.discard(task)
    def operation(self,kind,arguments,token=None,trace=None,*,timeout=None):
        trace=trace if trace is not None else {}
        seconds=self.config.timeouts['list' if kind=='list' else 'call']
        if timeout is not None:seconds=min(seconds,timeout)
        future=self.hub.submit(self._operation(kind,arguments,trace))
        try:return self._wait(future,seconds,token)
        except BaseException:
            self.state='failed';self.close();raise
    async def _close(self):
        if self.stop is not None:self.stop.set()
        if self.actor is not None:
            try:await asyncio.wait_for(asyncio.shield(self.actor),CLOSE_GRACE_SECONDS)
            except asyncio.TimeoutError:
                self.actor.cancel()
                try:await asyncio.wait_for(asyncio.shield(self.actor),CLOSE_CANCEL_SECONDS)
                except (asyncio.TimeoutError,asyncio.CancelledError):self.confirmed=False
        elif self.slot:
            if self.process is None and self.cleanup is None:
                self.confirmed=True;self.hub.capacity.release();self.slot=False
    def close(self):
        if self.closed:return self.confirmed and (self.actor is None or self.actor.done())
        self.closed=True
        if self.state!='failed':self.state='closing'
        try:
            if self.hub.loop is not None:self._wait(self.hub.submit(self._close()),CLOSE_WAIT_SECONDS)
            elif self.slot and self.actor is None and self.process is None:
                self.confirmed=True;self.hub.capacity.release();self.slot=False
        except BaseException:self.confirmed=False
        if self.confirmed:self.state='disconnected' if self.error is None else 'failed'
        else:self.state='failed'
        return self.confirmed
