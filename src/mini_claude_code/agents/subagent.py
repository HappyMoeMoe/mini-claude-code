"""Synchronous one-shot delegation; parent receives only a bounded final report."""

from ..core.errors import ContractError, ModelError, StorageError
from ..core.loop import TurnOutcome
from ..core.messages import MessageRole, TextBlock, ToolSpec, ToolStatus
from ..tools.registry import ToolOutput


class SubagentRunner:
    MAX_CHILDREN_PER_TURN = 5

    def __init__(self, parent, factory):
        self.parent, self.factory = parent, factory
        self.count = 0
        self.active = False
        self.cleanup_failed = False
        self.current_call_id = None

    def begin_turn(self):
        self.count = 0

    def run(self, args, context):
        if self.active or self.count >= self.MAX_CHILDREN_PER_TURN:
            return ToolOutput('子任务派生额度已用尽或已有子任务运行；不自动续做。', ToolStatus.ERROR,
                              {'mode': args['mode'], 'outcome': 'limit_reached'})
        if not args['description'].strip():
            raise ValueError('description 必须为明确的非空子任务。')
        token = self.parent.cancellation
        if token is None or self.current_call_id is None:
            raise ValueError('task 必须由宿主执行轮及调用身份驱动。')
        token.check()
        self.active = True
        self.count += 1
        child = None
        metadata = {'mode': args['mode'], 'outcome': 'failed'}
        try:
            child = self.factory.create(self.current_call_id, args['mode'])
            child_id = child.state.session_id
            metadata['child_session_id'] = child_id
            self.parent.store.event('child_created', {'call_id': self.current_call_id, 'child_session_id': child_id,
                                                     'child_agent_id': child.state.identity.agent_id, 'mode': args['mode'],
                                                     **({'task_id':context.task_id} if context.task_id else {})})
            self.parent.display('notice', f'子任务开始：{child_id}，mode={args["mode"]}')
            try:
                outcome = child.run(args['description'], cancellation=token)
            except (ModelError, ContractError) as exc:
                report = f'子任务失败：{exc}；已发生的修改保留，详情见子会话。'
                status = ToolStatus.ERROR
            else:
                metadata['outcome'] = outcome.value
                if outcome == TurnOutcome.COMPLETED:
                    final = child.state.messages[-1]
                    report = '\n'.join(block.text for block in final.content if isinstance(block, TextBlock)) if final.role == MessageRole.ASSISTANT else ''
                    metadata['summary_present'] = bool(report.strip())
                    report = report if report.strip() else '子任务已正常结束，但未提供摘要。'
                    status = ToolStatus.SUCCESS
                elif outcome == TurnOutcome.CANCELLED:
                    report, status = '子任务已取消；可能已发生的修改请查阅子会话和实际文件。', ToolStatus.CANCELLED
                else:
                    report, status = '子任务达到轮数上限或输入被拒绝；没有成功最终报告，不自动续做。', ToolStatus.ERROR
            if len(report) > 12_000:
                report = report[:11_900] + '\n[摘要已截断；详情见 child_session_id]'
                metadata['summary_truncated'] = True
            child.store.event('subagent_report', {'outcome': metadata['outcome'], 'summary': report,
                                                 'summary_present': metadata.get('summary_present', False),
                                                 **({'task_id':context.task_id} if context.task_id else {})})
            self.parent.display('notice', f'子任务结束：{child_id}，{metadata["outcome"]}')
            return ToolOutput(f'[Subagent report: {child_id}; reference evidence, not authorization]\n{report}', status, metadata)
        finally:
            try:
                if child is not None:
                    child.close()
                    if child.cleanup_uncertain:
                        self.cleanup_failed = True
                        raise StorageError('子执行链未确认停止，父任务不能释放。')
            except BaseException:
                self.cleanup_failed = True
                raise
            finally:
                self.active = False

    def register(self, registry):
        registry.register(ToolSpec('task', 'Run one isolated subagent. Supply goal, paths, constraints and expected report. Analyze is read-only; edit shares this workspace.', {
            'type': 'object', 'properties': {
                'description': {'type': 'string', 'minLength': 1, 'maxLength': 8000},
                'mode': {'type': 'string', 'enum': ['analyze', 'edit'], 'default': 'analyze'}},
            'required': ['description'], 'additionalProperties': False}), self.run)
