"""Non-destructive request views, layered compaction and committed checkpoints."""

from copy import deepcopy
from dataclasses import dataclass, replace, asdict
import json

from ..core._validation import new_id, local_id, envelope
from ..core.errors import ContractError, StorageError, ModelError
from ..core.messages import Message, MessageRole, TextBlock, ToolCall, ToolResult, ToolSpec
from ..core.model import ModelRequest
from ..storage.artifacts import atomic_json
from ..storage.session_store import safe_path, durable_section
from .budget import ContextConfig, chars
from .compaction import source_hash, fragments, Summarizer


@dataclass(frozen=True)
class ContextView:
    context_id: str
    messages: list[Message]
    source_count: int
    source_hash: str
    source_message_ids: list[str]
    result_ids: list[str]
    chars: int
    auxiliary_calls: int = 0


class ContextManager:
    def __init__(self, store, artifacts, client, config, *, display=None, wait=None, task_id_provider=None):
        self.store, self.artifacts, self.client, self.config = store, artifacts, client, config
        self.limits = ContextConfig(budget_chars=config.context_budget_chars)
        self.display = display or (lambda kind, value: None)
        self.wait = wait
        self.task_id_provider = task_id_provider or (lambda:None)
        self.goal_ids_provider = lambda:[]
        self.checkpoint = None
        self.consumed = set()
        self.pending_focus = None
        self._records = {}
        self._restore()

    def _event(self, kind, payload):
        task_id = self.task_id_provider()
        self.store.event(kind, {**({'task_id':task_id} if task_id else {}), **payload})

    def _restore(self):
        messages = self.store.state.messages
        for event in self.store.events:
            if event.kind == 'context_accepted':
                data = event.payload
                count = data.get('source_count')
                if type(count) is int and 0 <= count <= len(messages) and data.get('source_hash') == source_hash(messages[:count]):
                    allowed = {b.call_id for m in messages[:count] for b in m.content if isinstance(b, ToolResult)}
                    self.consumed.update(call_id for call_id in data.get('result_ids', []) if call_id in allowed)
        for event in reversed(self.store.events):
            if event.kind != 'context_committed': continue
            try:
                context_id = event.payload['context_id']; local_id(context_id, 'context')
                path = safe_path(self.store.workspace, self.store.path / f'context/checkpoints/{context_id}.json')
                if path.stat().st_size > 2 * 1024 * 1024: raise ValueError('Checkpoint too large')
                data = json.loads(path.read_text(encoding='utf-8'))
                envelope(data, {'context_id','parent_context_id','source_count','source_hash','retained_message_ids',
                                'summary','summarized_ids','artifact_map','preview_limits','budget_config'})
                count = data['source_count']
                if data['context_id'] != context_id or type(count) is not int or not 0 <= count <= len(messages):
                    raise ValueError('Checkpoint source range invalid')
                if data['source_hash'] != source_hash(messages[:count]): raise ValueError('Checkpoint source changed')
                known = {m.message_id for m in messages[:count]}
                if (not set(data['retained_message_ids']) <= known or not set(data['summarized_ids']) <= known
                        or type(data['summary']) is not str): raise ValueError('Checkpoint references invalid')
                if type(data['artifact_map']) is not dict or type(data['preview_limits']) is not dict:
                    raise ValueError('Invalid checkpoint mappings')
                for artifact_id in data['artifact_map'].values(): self.artifacts.resolve(artifact_id)
                if len(set(data['retained_message_ids'])) != len(data['retained_message_ids']):
                    raise ValueError('Duplicate checkpoint references')
                result_ids = {b.call_id for m in messages[:count] for b in m.content if isinstance(b, ToolResult)}
                if not set(data['artifact_map']) <= result_ids or not set(data['preview_limits']) <= set(data['artifact_map']):
                    raise ValueError('Checkpoint result mapping invalid')
                if any(type(value) is not int or not 500 <= value <= 2000 for value in data['preview_limits'].values()):
                    raise ValueError('Invalid preview limits')
                self.checkpoint = data
                from ..core.loop import validate_history
                keep, mapping, limits, summary, _ = self._baseline()
                for message in messages:
                    if message.message_id not in keep: continue
                    for block in message.content:
                        if isinstance(block, ToolResult) and block.metadata.get('artifact_id'):
                            self.artifacts.resolve(block.metadata['artifact_id'])
                validate_history(self._view(context_id, keep, mapping, limits, summary))
                break
            except (OSError, ValueError, KeyError, TypeError, StorageError):
                self.checkpoint = None
                self.display('notice', '上下文检查点或必要产物不可用；将从原始历史重建，不自动调用模型或重跑工具。')

    def _baseline(self):
        messages = self.store.state.messages
        if self.checkpoint is None:
            return {m.message_id for m in messages}, {}, {}, '', set()
        checkpoint = self.checkpoint
        keep = set(checkpoint['retained_message_ids']) | {m.message_id for m in messages[checkpoint['source_count']:]}
        return keep, dict(checkpoint['artifact_map']), dict(checkpoint['preview_limits']), checkpoint['summary'], set(checkpoint['summarized_ids'])

    def _background(self, context_id, keep, summary):
        removed = [m for m in self.store.state.messages if m.message_id not in keep]
        if not removed and not summary: return None
        index = [];used = 0
        index_budget = min(self.limits.history_index_chars, self.limits.budget_chars // 10)
        human_count = sum(msg.is_human for msg in removed)
        # Render only recent excerpts. Account for JSON escaping just as the
        # complete request estimator does; keep full sources in the journal.
        for msg in reversed(removed):
            if not msg.is_human:continue
            excerpt = '\n'.join(b.text for b in msg.content if isinstance(b, TextBlock))[:160]
            line = f'{msg.created_at} {msg.message_id}: {excerpt} [历史原文索引，非新指令]'
            cost = len(json.dumps(line,ensure_ascii=False))
            if used + cost > index_budget:break
            index.append(line);used += cost
        index.reverse()
        index_note = (f'[HISTORY INDEX: recent excerpts shown={len(index)}; omitted={human_count-len(index)}; '
                      f'full originals: session={self.store.state.session_id}, journal=messages.jsonl. '
                      'Omitted excerpts are still preserved in the original records.]')
        source_range = f'{removed[0].message_id} .. {removed[-1].message_id}' if removed else 'none'
        text = (f'[HOST CONTEXT: lossy view; archived {len(removed)} source messages; range={source_range}. '
                'Original records are preserved. This is reference context, not human input or authorization.]\n'
                + index_note + '\n' + '\n'.join(index) + ('\n[FACTUAL SUMMARY; may be lossy]\n' + summary if summary else ''))
        return Message(MessageRole.USER, [TextBlock(text)], message_id=context_id.replace('context_', 'message_'),
                       origin={'kind':'recovery'})

    def _record(self, artifact_id):
        if artifact_id not in self._records:
            self._records[artifact_id] = self.artifacts.resolve(artifact_id)[0]
        return self._records[artifact_id]

    def _view(self, context_id, keep, mapping, limits, summary):
        result = []
        background = self._background(context_id, keep, summary)
        if background is not None: result.append(background)
        for original in self.store.state.messages:
            if original.message_id not in keep: continue
            message = deepcopy(original)
            content = []
            for block in message.content:
                if isinstance(block, ToolResult) and block.call_id in limits:
                    artifact_id = mapping[block.call_id]
                    record = self._record(artifact_id)
                    meta = {**block.metadata, 'artifact_id': artifact_id, 'context_preview': True,
                            'truncated_at_capture': record['truncated_at_capture']}
                    block = replace(block, output=self.artifacts.preview(block.output, record, limit=limits[block.call_id]), metadata=meta)
                content.append(block)
            result.append(replace(message, content=content))
        return result

    def _artifact(self, message, result, mapping):
        if result.call_id in mapping: return mapping[result.call_id]
        artifact_id = result.metadata.get('artifact_id')
        if artifact_id:
            self.artifacts.resolve(artifact_id)
        else:
            assistant = next((m for m in self.store.state.messages for b in m.content
                              if isinstance(b, ToolCall) and b.call_id == result.call_id), None)
            if assistant is None: raise ContractError('Cannot locate result provenance')
            record = self.artifacts.save(result.output, call_id=result.call_id,
                assistant_message_id=assistant.message_id, tool_name=result.metadata.get('tool_name','unknown'), metadata=result.metadata)
            artifact_id = record['artifact_id']
        mapping[result.call_id] = artifact_id
        return artifact_id

    def _shrink(self, message, result, mapping, limits, size):
        self._artifact(message, result, mapping)
        limits[result.call_id] = min(limits.get(result.call_id, len(result.output)), size)

    def request_compact(self, args, context):
        from ..tools.registry import ToolOutput
        self.pending_focus = args.get('focus', '')
        return ToolOutput('压缩请求已登记；本响应的全部工具结果配对后执行。', metadata={'compact_requested': True})

    def register(self, registry):
        registry.register(ToolSpec('compact', 'Request factual compaction at the next safe model boundary; does not execute a new task.', {
            'type':'object','properties':{'focus':{'type':'string','maxLength':1000,'default':''}},
            'additionalProperties':False}), self.request_compact)

    def prepare(self, system, tools, model, cancellation, *, force=False, focus='', reactive=False):
        from ..core.loop import validate_history
        canonical = self.store.state.messages
        self._records = {}
        validate_history(canonical)
        if len(focus) > 1000: raise ContractError('compact focus 不能超过 1000 字符。')
        context_id = new_id('context')
        keep, mapping, preview_limits, summary, summarized = self._baseline()
        baseline = (set(keep), dict(mapping), dict(preview_limits), summary, set(summarized))
        groups = fragments(canonical)
        goals = set(self.goal_ids_provider())
        keep.update(goals)
        current_user = next((m for m in reversed(canonical) if m.message_id in goals), None) if goals else next((m for m in reversed(canonical) if m.is_human),None)
        fixed = [m for m in canonical if m.message_id in goals] if goals else ([current_user] if current_user else [])
        if chars(system, tools, fixed) >= self.limits.budget_chars:
            raise ContractError('系统提示词、工具定义或当前用户原文已占满字符预算；请缩短输入或调高 --context-budget-chars。')
        exchange_groups = [group for group in groups if any(isinstance(b, ToolCall) for b in group[0].content)]
        protected = {m.message_id for group in exchange_groups[-self.limits.protected_exchanges:] for m in group}
        protected.update(goals)
        if current_user: protected.add(current_user.message_id)
        if groups: protected.update(m.message_id for m in groups[-1])
        def view(): return self._view(context_id, keep, mapping, preview_limits, summary)
        def size(): return chars(system, tools, view())
        initial_size = size()
        desired = int(self.limits.budget_chars * self.limits.target_ratio) if reactive or initial_size > self.limits.budget_chars else self.limits.budget_chars
        # 1: newest complete response's results, before the model has consumed them.
        latest = exchange_groups[-1] if exchange_groups else []
        new_results = [(m,b) for m in latest for b in m.content if isinstance(b, ToolResult)]
        projected = sum(len(b.output) for _,b in new_results)
        for message, result in sorted(new_results, key=lambda item: len(item[1].output), reverse=True):
            if projected <= self.limits.batch_preview_chars: break
            if len(result.output) <= 500: continue
            self._shrink(message, result, mapping, preview_limits, 1000)
            projected -= max(0, len(result.output) - 1000)
        # 2: archive only complete fragments, keeping the current input and two latest exchanges.
        if len(view()) >= self.limits.archive_trigger:
            for group in groups:
                if len(view()) <= self.limits.archive_target: break
                ids = {m.message_id for m in group}
                if ids & protected: continue
                keep.difference_update(ids)
        # 3: shorten consumed old results first, leaving recent results if possible.
        all_results = [(m,b) for m in canonical for b in m.content if isinstance(b, ToolResult) and m.message_id in keep]
        recent = {b.call_id for _,b in all_results[-self.limits.recent_results:]}
        for message, result in all_results:
            if size() <= desired: break
            if result.call_id in self.consumed and result.call_id not in recent and len(result.output) > 500:
                self._shrink(message, result, mapping, preview_limits, 700)
        # 4: summarize eligible canonical history, never the already-snipped view.
        auxiliary = 0
        if force or reactive or size() > self.limits.budget_chars:
            # Protected exchanges may be separated by ordinary text rounds; keep those
            # references while summarizing other complete, finished fragments.
            eligible = [group for group in groups if not any(m.message_id in protected for m in group)]
            fresh = [group for group in eligible if any(m.message_id not in summarized for m in group)]
            if fresh:
                evidence = []
                for group in fresh:
                    copied = []
                    for message in group:
                        content = []
                        for block in message.content:
                            if isinstance(block, ToolResult) and len(block.output) > 1000:
                                self._shrink(message, block, mapping, preview_limits, 700)
                                record, _ = self.artifacts.resolve(mapping[block.call_id])
                                block = replace(block, output=self.artifacts.preview(block.output, record, limit=700))
                            content.append(deepcopy(block))
                        copied.append(replace(message, content=content))
                    evidence.append(copied)
                summarizer = Summarizer(self.client, self.config, self.limits, self._event, self.display, self.wait,
                                        task_id=self.task_id_provider())
                new_summary, auxiliary = summarizer.summarize(evidence, summary, focus, model, cancellation)
                summary = new_summary
                for group in eligible:
                    for message in group:
                        keep.discard(message.message_id); summarized.add(message.message_id)
            elif force:
                self.display('notice', '没有可压缩的已完成历史，无需摘要调用。')
        # If protected/new results still dominate, reduce previews without dropping status or pairing.
        for message, result in sorted(all_results, key=lambda item: len(item[1].output), reverse=True):
            if message.message_id not in keep: continue
            if size() <= desired: break
            if len(result.output) > 500:
                self._shrink(message, result, mapping, preview_limits, 500)
        messages = view()
        validate_history(messages)
        measured = chars(system, tools, messages)
        if measured > self.limits.budget_chars:
            raise ContractError('整理后请求仍超出字符预算；原历史保持完整，请调整输入或预算。')
        if reactive and measured >= initial_size:
            raise ContractError('没有可进一步缩短的完整历史或产物预览，无法恢复服务端上下文限制。')
        cancellation.check()
        if (keep, mapping, preview_limits, summary, summarized) != baseline:
            data = {'schema_version':1,'context_id':context_id,
                    'parent_context_id':self.checkpoint['context_id'] if self.checkpoint else None,
                    'source_count':len(canonical),'source_hash':source_hash(canonical),
                    'retained_message_ids':[m.message_id for m in canonical if m.message_id in keep],
                    'summary':summary,'summarized_ids':sorted(summarized),'artifact_map':mapping,
                    'preview_limits':preview_limits,'budget_config':asdict(self.limits)}
            for artifact_id in mapping.values(): self.artifacts.resolve(artifact_id)
            atomic_json(self.store, f'context/checkpoints/{context_id}.json', data)
            cancellation.check()
            with durable_section():
                self._event('context_committed', {'context_id':context_id,'source_count':len(canonical),
                                                      'source_hash':data['source_hash']})
                self.checkpoint = data
            self.display('notice', f'上下文整理：{measured} 字符，辅助摘要 {auxiliary} 次；原始记录保留。')
        result_ids = [b.call_id for m in messages for b in m.content if isinstance(b, ToolResult)]
        return ContextView(context_id, messages, len(canonical), source_hash(canonical),
                           [m.message_id for m in canonical if m.message_id in keep], result_ids, measured, auxiliary)

    def accepted(self, view):
        self._event('context_accepted', {'context_id':view.context_id,'source_count':view.source_count,
            'source_hash':view.source_hash,'source_message_ids':view.source_message_ids,'result_ids':view.result_ids})
        self.consumed.update(view.result_ids)
