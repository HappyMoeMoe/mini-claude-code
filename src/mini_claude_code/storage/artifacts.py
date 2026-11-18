"""Bounded text artifacts, registered provenance and narrow session-scoped reads."""

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading

from ..core._validation import new_id, local_id, envelope
from ..core.errors import StorageError
from ..core.messages import ToolSpec, ToolCall
from ..tools.registry import ToolOutput
from .session_store import SessionStore, safe_path

SINGLE_LIMIT = 16 * 1024 * 1024
SESSION_LIMIT = 128 * 1024 * 1024


def atomic_json(store, relative, data):
    store._check()
    path = safe_path(store.workspace, store.path / relative)
    name = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, name = tempfile.mkstemp(prefix='.pending-', dir=path.parent)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(data, stream, ensure_ascii=False, allow_nan=False)
            stream.flush(); os.fsync(stream.fileno())
        os.replace(name, path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(fd)
        finally: os.close(fd)
    except (OSError, ValueError, StorageError) as exc:
        store.failed = True
        raise StorageError('产物或上下文记录无法可靠保存；停止执行。') from exc
    finally:
        if name is not None:
            Path(name).unlink(missing_ok=True)


class ArtifactStore:
    def __init__(self, store):
        self.store = store
        self.root = safe_path(store.workspace, store.path / 'outputs')
        self.quota_lock = threading.RLock()
        self.external_usage = lambda:0
        self.foreground_capture = 0

    def usage(self):
        safe_path(self.store.workspace, self.root)
        if not self.root.exists():
            return 0
        total = 0
        for path in self.root.iterdir():
            safe_path(self.store.workspace, path)
            if not path.is_file():
                raise StorageError('输出产物目录包含非文件记录。')
            total += path.stat().st_size
        return total

    def check_capture(self, captured_bytes):
        try:
            with self.quota_lock:
                if self.usage() + captured_bytes + self.external_usage() > SESSION_LIMIT:
                    raise StorageError('输出捕获超过会话 128 MiB 配额。')
                self.foreground_capture = captured_bytes
        except OSError as exc:
            raise StorageError('无法计算输出配额。') from exc

    def capture_closed(self):
        with self.quota_lock:self.foreground_capture=0

    def save(self, text, *, call_id, assistant_message_id, tool_name, metadata=None, transient_bytes=0):
        with self.quota_lock:
            return self._save(text,call_id=call_id,assistant_message_id=assistant_message_id,tool_name=tool_name,
                              metadata=metadata,transient_bytes=transient_bytes+self.external_usage())

    def _save(self, text, *, call_id, assistant_message_id, tool_name, metadata=None, transient_bytes=0):
        self.store._check()
        original = text.encode('utf-8')
        stored = original[:SINGLE_LIMIT].decode('utf-8', errors='ignore').encode('utf-8')
        artifact_id = new_id('artifact')
        details = dict(metadata or {})
        capture_truncated = bool(details.get('truncated_at_capture') or len(original) > len(stored)
                                 or (details.get('truncated') and not details.get('artifact_id')))
        record = {'schema_version': 1, 'artifact_id': artifact_id,
                  'session_id': self.store.state.session_id, 'agent_id': self.store.state.identity.agent_id,
                  'assistant_message_id': assistant_message_id, 'call_id': call_id, 'tool_name': tool_name,
                  'bytes_stored': len(stored), 'bytes_total': details.get('bytes_total', len(original)),
                  'characters_stored': len(stored.decode('utf-8')), 'sha256': hashlib.sha256(stored).hexdigest(),
                  'truncated_at_capture': capture_truncated}
        name = None
        try:
            overhead = len(json.dumps(record, ensure_ascii=False).encode('utf-8')) + 1024
            if self.usage() + transient_bytes + len(stored) + overhead > SESSION_LIMIT:
                raise StorageError('会话 128 MiB 配额不足以保存必要输出，不删除旧产物或重跑命令。')
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd, name = tempfile.mkstemp(prefix='.capture-', dir=self.root)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(stored); stream.flush(); os.fsync(stream.fileno())
            os.replace(name, safe_path(self.store.workspace, self.root / f'{artifact_id}.txt'))
            atomic_json(self.store, f'outputs/{artifact_id}.json', record)
            self.store.event('artifact_saved', {'artifact_id': artifact_id, 'assistant_message_id': assistant_message_id,
                                               'call_id': call_id, 'bytes_stored': len(stored),
                                               **({'task_id':details['task_id']} if details.get('task_id') else {})})
            return record
        except (OSError, ValueError, StorageError) as exc:
            self.store.failed = True
            raise StorageError('工具输出已产生，但产物保存失败；停止后续操作。') from exc
        finally:
            if name is not None: Path(name).unlink(missing_ok=True)

    def preview(self, text, record, *, limit=2000):
        note = (f'\n[省略输出；artifact_id={record["artifact_id"]}; stored={record["bytes_stored"]} bytes; '
                f'truncated_at_capture={record["truncated_at_capture"]}; '
                'use read_tool_output(artifact_id, offset=0, limit=200)]')
        room = max(0, limit - len(note))
        return text[:room * 3 // 4] + note + (text[-(room // 4):] if room // 4 else '')

    def transform(self, result, call, assistant_message_id, *, transient_bytes=0):
        if call.name == 'read_tool_output' or result.metadata.get('artifact_id'):
            return result
        if len(result.output) <= 30_000 and not result.metadata.get('truncated_at_capture'):
            return result
        record = self.save(result.output, call_id=call.call_id, assistant_message_id=assistant_message_id,
                           tool_name=call.name, metadata=result.metadata, transient_bytes=transient_bytes)
        metadata = {**result.metadata, 'artifact_id': record['artifact_id'], 'bytes_stored': record['bytes_stored'],
                    'bytes_total': record['bytes_total'], 'truncated': True,
                    'truncated_at_capture': record['truncated_at_capture']}
        return replace(result, output=self.preview(result.output, record), metadata=metadata)

    def shell_sink(self, output, context, invocation, transient_bytes):
        from ..core.messages import ToolResult
        call, message_id = invocation
        result = self.transform(ToolResult(call.call_id, output.status, output.output,
                                          {'tool_name': call.name, **({'task_id':context.task_id} if context.task_id else {}),
                                           **(output.metadata or {})}),
                                call, message_id, transient_bytes=transient_bytes)
        return ToolOutput(result.output, result.status, result.metadata)

    def _owners(self):
        yield self.store.state.session_id
        if self.store.state.identity.role.value != 'lead':
            return
        calls = {b.call_id for m in self.store.state.messages for b in m.content
                 if isinstance(b, ToolCall) and b.name == 'task'}
        for row in SessionStore.list_sessions(self.store.workspace, include_children=True):
            if (row['identity']['role'] == 'subagent' and row.get('parent_session_id') == self.store.state.session_id and row.get('parent_call_id') in calls
                    and row['identity'].get('parent_agent_id') == self.store.state.identity.agent_id):
                yield row['session_id']

    def resolve(self, artifact_id):
        local_id(artifact_id, 'artifact')
        for session_id in self._owners():
            root = self.store.path.parent / session_id / 'outputs'
            path = safe_path(self.store.workspace, root / f'{artifact_id}.json')
            if not path.exists():
                continue
            try:
                if path.stat().st_size > 16_384:
                    raise ValueError('Artifact metadata too large')
                record = json.loads(path.read_text(encoding='utf-8'))
                envelope(record, {'artifact_id','session_id','agent_id','assistant_message_id','call_id','tool_name',
                                  'bytes_stored','bytes_total','characters_stored','sha256','truncated_at_capture'})
                if record['artifact_id'] != artifact_id or record['session_id'] != session_id:
                    raise ValueError('Artifact owner mismatch')
                owner = SessionStore(self.store.workspace, session_id)._metadata()
                if record['agent_id'] != owner['identity']['agent_id']:
                    raise ValueError('Artifact agent mismatch')
                text_path = safe_path(self.store.workspace, root / f'{artifact_id}.txt')
                size = text_path.stat().st_size
                if type(record['bytes_stored']) is not int or not 0 <= size == record['bytes_stored'] <= SINGLE_LIMIT:
                    raise ValueError('Artifact size mismatch')
                digest = hashlib.sha256()
                with text_path.open('rb') as stream:
                    for chunk in iter(lambda: stream.read(65536), b''): digest.update(chunk)
                if digest.hexdigest() != record['sha256']:
                    raise ValueError('Artifact content mismatch')
                return record, text_path
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise ValueError('产物缺失、损坏或所属信息不匹配；不会重执行原工具。') from exc
        raise ValueError('当前会话及其授权子会话中没有此注册产物；不会重新执行工具。')

    def read(self, args, context):
        record, path = self.resolve(args['artifact_id'])
        offset, limit, char_offset = args['offset'], args['limit'], args['char_offset']
        output, used, has_more = [], 0, False
        next_offset, next_char_offset = offset, char_offset
        first_found = False
        with path.open('r', encoding='utf-8', newline='') as stream:
            for index, line in enumerate(stream):
                if index < offset: continue
                position = char_offset if index == offset else 0
                first_found = True
                if position > len(line):
                    raise ValueError('char_offset 超出指定行的字符长度。')
                if index >= offset + limit:
                    has_more = True
                    next_offset, next_char_offset = index, position
                    break
                prefix = f'{index + 1}' + (f' [char_offset={position}]' if position else '') + ': '
                room = 48_000 - used - len(prefix)
                if room <= 0:
                    has_more = True
                    next_offset, next_char_offset = index, position
                    break
                piece = line[position:position + room]
                output.append(prefix + piece)
                used += len(prefix) + len(piece)
                if position + len(piece) < len(line):
                    has_more = True
                    next_offset, next_char_offset = index, position + len(piece)
                    break
                next_offset, next_char_offset = index + 1, 0
        if not first_found and char_offset:
            raise ValueError('指定行不存在，不能应用 char_offset。')
        header = f'[artifact={record["artifact_id"]}; bytes={record["bytes_stored"]}; truncated_at_capture={record["truncated_at_capture"]}]\n'
        return ToolOutput(header + ''.join(output) +
            f'\n[has_more={has_more}; next_offset={next_offset}; next_char_offset={next_char_offset}]', metadata={
            'artifact_id': record['artifact_id'], 'offset': offset, 'char_offset': char_offset, 'limit': limit,
            'has_more': has_more, 'next_offset': next_offset, 'next_char_offset': next_char_offset,
            'truncated_at_capture': record['truncated_at_capture']})

    def register(self, registry):
        registry.register(ToolSpec('read_tool_output', 'Read a registered output artifact by zero-based line offset; char_offset resumes within that line. Follow next_offset and next_char_offset for complete paging.', {
            'type':'object','properties':{'artifact_id':{'type':'string','minLength':1},
                'offset':{'type':'integer','minimum':0,'default':0},
                'char_offset':{'type':'integer','minimum':0,'default':0},
                'limit':{'type':'integer','minimum':1,'maximum':2000,'default':200}},
            'required':['artifact_id'],'additionalProperties':False}), self.read)
