"""Host-only creation of linked child sessions with narrowed capabilities."""

from dataclasses import dataclass

from ..core._validation import new_id
from ..core.session import AgentIdentity, AgentRole
from ..providers.deepseek import DeepSeekClient


@dataclass(frozen=True)
class ChildSessionSpec:
    identity: AgentIdentity
    parent_session_id: str
    parent_call_id: str
    mode: str
    allowed_tools: frozenset[str]

    def linkage(self):
        return {'parent_session_id': self.parent_session_id,
                'parent_call_id': self.parent_call_id, 'mode': self.mode}


class AgentSessionFactory:
    def __init__(self, parent, client_factory=None):
        self.parent = parent
        self.client_factory = client_factory or DeepSeekClient

    def create_teammate(self, spec, *, display):
        """Called in the new owner thread; never borrow the lead assignment/history/client."""
        from ..runtime import Runtime
        if self.parent.state.identity.role != AgentRole.LEAD:
            raise ValueError('只有 lead 可以创建持久队友。')
        return Runtime(self.parent.config, interactive=False, display=display,
            max_rounds=self.parent.loop.max_rounds, stream=False, teammate_spec=spec,
            client_factory=self.client_factory,
            mcp_config=self.parent.mcp.config if self.parent.mcp else None,
            mcp_parent=self.parent.mcp,
            mcp_connection_factory=self.parent.mcp.factory if self.parent.mcp else None)

    def create(self, call_id, mode):
        from ..runtime import Runtime
        parent = self.parent
        if parent.state.identity.role != AgentRole.LEAD:
            raise ValueError('子 Agent 不允许再次派生，最大深度为 1。')
        names = {'read_file', 'glob', 'load_skill', 'read_tool_output', 'compact'}
        if mode == 'edit':
            names |= {'write_file', 'edit_file', 'bash'}
        elif mode != 'analyze':
            raise ValueError('未知子任务模式。')
        names &= {tool.name for tool in parent.registry.specs()}
        spec = ChildSessionSpec(AgentIdentity(new_id('agent'), AgentRole.SUBAGENT, parent.state.identity.agent_id),
                                parent.state.session_id, call_id, mode, frozenset(names))
        def display(kind, value):
            # Full text lives in the child journal; only summaries reach the terminal.
            if kind in {'call', 'result', 'notice'}:
                parent.display('notice', f'[subagent {spec.identity.agent_id}]')
                parent.display(kind, value)
        def approve(call, arguments, context):
            parent.display('notice', f'Shell 请求来自子 Agent {spec.identity.agent_id}，mode={mode}')
            return parent.executor.approve(call, arguments, context) if parent.executor.approve else False
        child = Runtime(parent.config, interactive=parent.context.interactive, approve=approve, display=display,
                       max_rounds=15, stream=parent.loop.stream, hooks=parent.hooks, wait=parent.loop.wait,
                       child_spec=spec, parent_runtime=parent, child_client_factory=self.client_factory,
                       client_factory=self.client_factory,
                       mcp_config=self.parent.mcp.config if self.parent.mcp else None,
                       mcp_parent=self.parent.mcp,
                       mcp_connection_factory=self.parent.mcp.factory if self.parent.mcp else None)

        child.mcp_approve = parent.mcp_approve
        return child
