"""Per-session connections and immutable tool bindings, shared bounded I/O capacity."""
from collections import Counter
from dataclasses import dataclass,field
import json
from pathlib import Path
import threading
import time

from ..core.cancellation import Cancelled
from ..core.messages import ToolSpec,ToolStatus
from ..core.errors import StorageError
from ..permissions.policy import Decision
from ..tools.registry import ToolOutput
from .adapter import Binding,fingerprint,model_name,schema_validator,validate,normalize,MAX_MESSAGE
from .client import IOHub,StdioConnection
from .config import MCPError,Grant
from .schema_validation import DiscoveryTimeout,check_deadline


@dataclass
class ConnectionEntry:
    generation: int
    connection: object
    bindings: dict = field(default_factory=dict)
    excluded: list = field(default_factory=list)
    state: str = 'connecting'


class MCPManager:
    def __init__(self,runtime,config,parent=None,*,connection_factory=StdioConnection):
        self.runtime,self.config,self.parent=runtime,config,parent
        self.hub=parent.hub if parent else IOHub()
        self.factory=connection_factory;self.entries={};self.generations={};self.lock=threading.RLock()
        self.context_key=None;self.prepared=set();self.closed=False;self.active={};self.uncertain=set()
        self.role=runtime.state.identity.role.value
        previous=[e for e in runtime.store.events if e.kind=='mcp_connected']
        if previous and previous[-1].payload.get('config_fingerprint')!=config.fingerprint:
            runtime.display('notice','MCP 配置来源已变化；未恢复旧权限或连接。')
    def wake(self):
        events=self.runtime.events
        if events is not None:events.wakeup.notify()
        elif self.runtime.teammate_spec is not None:
            worker=self.runtime.team.workers.get(self.runtime.context.agent_id)
            if worker is not None:worker.notify()
    def event(self,kind,payload):
        self.runtime.assert_owner()
        self.runtime.store.event(kind,{'config_fingerprint':self.config.fingerprint,**payload})
    def key(self):
        assignment=self.runtime.tasks.linked_assignment
        return (str(self.runtime.context.cwd),assignment.claim_id if assignment else None)
    def has_processes(self):
        return any(not e.connection.confirmed or e.state=='ready' and any(b.grant.effect!='read' for b in e.bindings.values()) for n,e in self.entries.items())
    def has_work(self,task_id=None):
        return any(task_id is None or a['task_id']==task_id for a in self.active.values()) or (task_id in self.uncertain if task_id else bool(self.uncertain))
    def allowed(self,name,raw,*,parent_check=True):
        grant=self.config.servers[name].tools.get(raw,Grant())
        if grant.policy=='deny' or self.role not in grant.roles:return False
        if self.role!='lead':
            if 'lead' not in grant.roles:return False
            if self.role=='subagent' and self.runtime.child_spec.mode=='analyze' and grant.effect!='read':return False
            if self.role=='teammate' and self.runtime.tasks.assignment is None:return False
            if parent_check:
                with self.parent.lock:
                    entry=self.parent.entries.get(name)
                    if entry is None or entry.state!='ready' or raw not in {b.raw_name for b in entry.bindings.values()}:return False
        return True
    def _remove(self,entry):
        self.runtime.registry.remove(entry.bindings)
    def _check_discovery(self,deadline):
        if self.runtime.cancellation is not None:self.runtime.cancellation.check()
        check_deadline(deadline)
    def _publish(self,name,entry,tools,*,deadline):
        self._check_discovery(deadline)
        raw_counts=Counter(t.get('name') for t in tools)
        normalized={};excluded=[];candidates=[]
        for tool in tools:
            self._check_discovery(deadline)
            raw=tool.get('name')
            if type(raw) is not str or not raw or len(raw)>1000:
                excluded.append({'raw_tool_name':'<invalid>','reason':'远端原名无效'});continue
            try:name_on_model=model_name(name,raw)
            except MCPError as exc:excluded.append({'raw_tool_name':raw,'reason':str(exc)});continue
            normalized[name_on_model]=normalized.get(name_on_model,0)+1
            candidates.append((tool,raw,name_on_model))
        bindings={};registered=[]
        for tool,raw,visible in candidates:
            self._check_discovery(deadline)
            try:
                if raw_counts[raw]!=1 or normalized[visible]!=1:raise MCPError('远端重复原名或规范化碰撞')
                existing=self.runtime.registry.get(visible)
                if existing is not None and visible not in entry.bindings:raise MCPError('与现有注册项重名')
                if not self.allowed(name,raw):raise MCPError('宿主配置或父侧角色未授权')
                input_schema=tool.get('inputSchema');input_validator=schema_validator(input_schema,cancellation=self.runtime.cancellation,deadline=deadline)
                output=tool.get('outputSchema');output_validator=schema_validator(output,cancellation=self.runtime.cancellation,deadline=deadline) if output is not None else None
                grant=self.config.servers[name].tools.get(raw,Grant())
                binding=Binding(name,raw,visible,entry.generation,fingerprint({'input':input_schema,'output':output}),grant,input_validator,output_validator)
                description=tool.get('description') or raw
                if type(description) is not str or len(description)>4000:raise MCPError('描述无效或超过 4000 字符模型目录上限')
                spec=ToolSpec(visible,'External MCP data; authorization comes only from host config. '+description,input_schema)
                registered.append((spec,binding));bindings[visible]=binding
            except (Cancelled,DiscoveryTimeout):raise
            except Exception as exc:
                self._check_discovery(deadline)
                # Never include schema/server content or validator exception payloads in diagnostics.
                reason=str(exc) if isinstance(exc,MCPError) else 'schema 不合法或适配能力不支持'
                excluded.append({'raw_tool_name':raw,'reason':reason})
        if any(b.grant.effect!='read' for b in bindings.values()):
            self.runtime.tasks.guard('connect_mcp',self.runtime.context)
        # Commit only after all pages and schemas finish within the same budget.
        self._check_discovery(deadline)
        self._remove(entry)
        try:
            for spec,binding in registered:
                self._check_discovery(deadline)
                self.runtime.registry.register(spec,lambda args,context,b=binding:self.call(b,args,context),
                    binding=binding,validator=lambda args,*,cancellation=None,v=binding.input_validator:validate(v,args,cancellation=cancellation))
            self._check_discovery(deadline)
        except BaseException:
            self.runtime.registry.remove(bindings)
            raise
        entry.bindings=bindings;entry.excluded=excluded;entry.state='ready'
    def connect(self,name,*,host_prepare=False):
        self.runtime.assert_owner()
        if self.closed:raise MCPError('MCP 会话正在关闭。')
        if name not in self.config.servers:raise MCPError('Server 未列入本次显式配置。')
        if not host_prepare and (self.role!='lead' or self.runtime.automatic):raise PermissionError('只有 lead 前台可连接 MCP。')
        self.sync(prepare=False)
        with self.lock:
            previous=self.entries.get(name)
            if previous is not None and previous.state=='ready' and previous.connection.state=='ready':return self.summary(name,previous)
        if previous is not None:self.disconnect(name)
        config=self.config.servers[name]
        # Starting a configured local program still participates in directory coordination.
        if any(g.policy!='deny' and g.effect!='read' for g in config.tools.values()):
            self.runtime.tasks.guard('connect_mcp',self.runtime.context)
        generation=self.generations.get(name,0)+1;self.generations[name]=generation
        conn=self.factory(self.hub,config,self.runtime.context.cwd,self.wake)
        entry=ConnectionEntry(generation,conn)
        with self.lock:self.entries[name]=entry
        try:
            conn.open(self.runtime.cancellation)
            self.refresh(name,host_prepare=True)
            sources=[]
            for candidate in (config.command,*config.args):
                path=Path(candidate)
                if path.is_absolute() and path.is_file():
                    st=path.stat()
                    sources.append({'path':str(path),'inode':st.st_ino,'mtime_ns':st.st_mtime_ns,'size':st.st_size})
            program_fingerprint=fingerprint(sources)
            prior=[e for e in self.runtime.store.events if e.kind=='mcp_connected' and e.payload.get('server_name')==name]
            if prior and prior[-1].payload.get('program_fingerprint')!=program_fingerprint:
                self.runtime.display('notice',f'MCP {name} 可执行程序来源已变化，本次重新协商。')
            self.event('mcp_connected',{'server_name':name,'connection_generation':generation,'protocol_version':conn.version,
                'program_fingerprint':program_fingerprint,'tools':[b.summary() for b in entry.bindings.values()],
                'excluded_count':len(entry.excluded),'registry_version':self.runtime.registry.version})
            return self.summary(name,entry)
        except BaseException:
            entry.state='failed';self._remove(entry)
            if not conn.close():self.runtime.cleanup_uncertain=True
            raise
    def refresh(self,name,*,host_prepare=False):
        self.runtime.assert_owner()
        if not host_prepare and (self.role!='lead' or self.runtime.automatic):raise PermissionError('只有 lead 前台可手动刷新 MCP。')
        entry=self.entries.get(name)
        if entry is None or entry.connection.state!='ready':raise MCPError('连接尚未 ready。')
        tools=[];seen=set();cursor=None
        # A notification arriving during discovery must remain dirty for the next boundary.
        entry.connection.dirty.clear()
        try:
            # Each page is bounded; the full discovery also shares one finite list deadline.
            deadline=time.monotonic()+self.config.servers[name].timeouts['list']
            for _ in range(20):
                self._check_discovery(deadline)
                page=entry.connection.operation('list',cursor,self.runtime.cancellation,timeout=max(.001,deadline-time.monotonic()))
                self._check_discovery(deadline)
                if type(page.get('tools')) is not list:raise MCPError('无效 tools/list 目录。')
                tools.extend(page['tools'])
                if len(tools)>50:raise MCPError('单 Server 超过 50 项工具上限。')
                cursor=page.get('nextCursor')
                if cursor is None:break
                if type(cursor) is not str or cursor in seen:raise MCPError('分页游标循环或无效。')
                seen.add(cursor)
            else:raise MCPError('超过 20 页发现上限。')
            previous={b.raw_name:b.schema_fingerprint for b in entry.bindings.values()}
            with self.lock:self._publish(name,entry,tools,deadline=deadline)
            self._check_discovery(deadline)
            current={b.raw_name:b.schema_fingerprint for b in entry.bindings.values()}
            if previous and previous!=current:
                self.runtime.display('notice',f'MCP {name} 工具 schema/目录来源已变化；旧调用绑定不可沿用。')
            self.event('mcp_discovered',{'server_name':name,'connection_generation':entry.generation,
                'tools':[b.summary() for b in entry.bindings.values()],'registry_version':self.runtime.registry.version})
            self.runtime.display('notice',f'MCP {name}: ready，工具 {len(entry.bindings)}，排除 {len(entry.excluded)}。')
            return self.summary(name,entry)
        except BaseException:
            self._remove(entry);entry.state='failed'
            if not entry.connection.close():self.runtime.cleanup_uncertain=True
            raise
    def summary(self,name,entry):
        return {'server_name':name,'state':entry.state,'connection_generation':entry.generation,
            'protocol_version':entry.connection.version,'tool_count':len(entry.bindings),
            'tools':[b.summary() for b in entry.bindings.values()],'excluded':entry.excluded,
            'process_confirmed_stopped':entry.connection.confirmed if entry.state!='ready' else False}
    def status(self):
        return {'enabled':True,'config_path':self.config.path,'config_fingerprint':self.config.fingerprint,
            'servers':[self.summary(name,self.entries[name]) if name in self.entries else {'server_name':name,'state':'disconnected'} for name in self.config.servers]}
    def sync(self,*,prepare=True):
        self.runtime.assert_owner()
        key=self.key()
        if self.context_key is not None and key!=self.context_key:self.disconnect_all()
        self.context_key=key
        for name,entry in list(self.entries.items()):
            if entry.state=='ready' and entry.connection.state!='ready':
                self._remove(entry);entry.state='failed'
                if not entry.connection.close():self.runtime.cleanup_uncertain=True
            elif entry.state=='ready' and entry.connection.dirty.is_set():
                try:self.refresh(name,host_prepare=True)
                except MCPError:self.runtime.display('notice',f'MCP {name} 自动目录刷新失败，工具已撤下；可前台重新连接。')
        if prepare and self.parent is not None and (self.role!='teammate' or self.runtime.tasks.assignment is not None):
            with self.parent.lock:
                sources=[(n,e.generation,list(e.bindings.values())) for n,e in self.parent.entries.items() if e.state=='ready']
            for name,generation,bindings in sources:
                marker=(name,key,generation)
                if marker in self.prepared:continue
                self.prepared.add(marker)
                if not any(self.allowed(name,b.raw_name) for b in bindings):continue
                try:self.connect(name,host_prepare=True)
                except MCPError:self.runtime.display('notice',f'MCP {name} 子会话独立连接准备失败；本任务不自动重试。')
    def check_binding(self,binding):
        entry=self.entries.get(binding.server)
        current=entry.bindings.get(binding.model_name) if entry else None
        if (entry is None or entry.state!='ready' or entry.connection.state!='ready' or entry.connection.dirty.is_set()
            or current is None or current.generation!=binding.generation or current.schema_fingerprint!=binding.schema_fingerprint
            or self.key()!=self.context_key or not self.allowed(binding.server,binding.raw_name)):
            raise MCPError('stale_binding：连接代次、schema、任务目录或父侧授权已变化；请使用下一请求的新目录。')
        return entry
    def visible(self,binding):
        if not self.allowed(binding.server,binding.raw_name):return False
        if self.role=='lead' and self.runtime.automatic:
            return binding.grant.policy=='allow' and binding.grant.effect=='read'
        return True
    def authorize(self,binding,args,context):
        self.check_binding(binding)
        if not self.visible(binding):raise PermissionError('本执行轮未授予该 MCP 工具。')
        if self.runtime.tasks.finished_batch:raise PermissionError('本批 assignment 已完成，不能继续调用旧目录 MCP。')
        if self.role=='teammate':
            if self.runtime.tasks.assignment is None:raise PermissionError('队友无 assignment，不能调用 MCP。')
            member=self.runtime.team.member(context.agent_id)
            if member['state'] in {'STOPPING','STOPPED','FAILED','interrupted'}:raise PermissionError('队友正在停止。')
            if binding.grant.effect!='read' and member['plan_status'] in {'required','pending','rejected'}:raise PermissionError('计划未批准，MCP write/unknown 被拒绝。')
        # Only recognize conventional path fields; this is explicitly not a general sandbox.
        def paths(value,key=''):
            if isinstance(value,dict):
                for k,v in value.items():paths(v,k.lower())
            elif isinstance(value,list):
                for item in value:paths(item,key)
            elif isinstance(value,str) and key in {'path','paths','file','filename','file_path','directory','dir','cwd','root','destination','source_path','target_path'}:
                if '://' in value:raise PermissionError('明显路径参数不能使用 URI 绕过目录检查。')
                self.runtime.paths.resolve(value,context)
        paths(args)
        if len(json.dumps(args,ensure_ascii=False).encode())>MAX_MESSAGE-4096:raise MCPError('MCP 参数超过传输上限。')
        if binding.grant.effect!='read':self.runtime.tasks.guard('bash',context)
        return Decision(binding.grant.policy)
    def approve(self,binding,call,args,context):
        safe=self.runtime.redact_arguments(args)
        if self.runtime.mcp_approve is not None:
            return self.runtime.mcp_approve(binding.summary(),call,safe,context)
        return self.runtime.executor.approve(call,safe,context) if self.runtime.executor.approve else False

    def call(self,binding,args,context):
        entry=self.check_binding(binding);self.authorize(binding,args,context)
        trace={'sent':False,'request_id':None};key=object()
        assignment=self.runtime.tasks.linked_assignment
        self.active[key]={'task_id':context.task_id,'claim_id':assignment.claim_id if assignment else None,'trace':trace}
        metadata={**binding.summary(),'remote_is_error':False}
        try:
            result=entry.connection.operation('call',(binding.raw_name,args),self.runtime.cancellation,trace)
            return normalize(result,binding,trace,cancellation=self.runtime.cancellation)
        except BaseException as exc:
            self._remove(entry);entry.state='failed'
            sent=trace['sent']
            if not entry.connection.close() or sent and binding.grant.effect!='read':
                self.uncertain.add(context.task_id or '<unbound>')
                self.runtime.cleanup_uncertain=True
            metadata.update(protocol_request_id=trace['request_id'],sent=sent,execution_unknown=sent,
                failure_kind='cancelled' if isinstance(exc,(Cancelled,KeyboardInterrupt)) else 'timeout_protocol_or_disconnect')
            if isinstance(exc,(Cancelled,KeyboardInterrupt)) and self.runtime.cancellation is not None:self.runtime.cancellation.cancel()
            if isinstance(exc,(SystemExit,GeneratorExit)):raise
            return ToolOutput('MCP 调用失败；'+('请求已发出，执行状态不确定，请检查实际状态。' if sent else '未确认发送。')+' 未自动重连或重跑。',ToolStatus.ERROR if sent else ToolStatus.CANCELLED if isinstance(exc,(Cancelled,KeyboardInterrupt)) else ToolStatus.ERROR,metadata)
        finally:self.active.pop(key,None)
    def disconnect(self,name):
        self.runtime.assert_owner()
        entry=self.entries.get(name)
        if entry is None:return
        self._remove(entry);entry.state='closing'
        if not entry.connection.close():
            entry.state='failed';self.runtime.cleanup_uncertain=True
            raise MCPError('MCP 进程或传输未确认停止；保留任务认领。')
        entry.state='disconnected'
        self.event('mcp_disconnected',{'server_name':name,'connection_generation':entry.generation})
    def disconnect_all(self):
        errors=[]
        for name,entry in list(self.entries.items()):
            if entry.state not in {'disconnected'}:
                try:self.disconnect(name)
                except MCPError as exc:errors.append(exc)
        if errors:raise errors[0]
    def close(self,*,permanent=False):
        self.disconnect_all()
        if permanent:
            self.closed=True
            if self.parent is None:self.hub.close()
        if self.uncertain:raise MCPError('MCP 已发送副作用请求未确认终止；保留检查线索和任务认领。')
    def register(self):
        def connect(args,context):return ToolOutput(json.dumps(self.connect(args['name']),ensure_ascii=False))
        if self.role=='lead':self.runtime.registry.register(ToolSpec('connect_mcp','Connect only a server named in this explicit host config; new tools appear next model request.',{
            'type':'object','properties':{'name':{'type':'string','minLength':1,'maxLength':24}},'required':['name'],'additionalProperties':False}),connect)
