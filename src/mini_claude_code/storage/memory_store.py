"""Workspace memory: one validated JSON authority and short locked transactions."""

from copy import deepcopy
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
import unicodedata

from ..core._validation import envelope, fields, integer, local_id, new_id, timestamp, utc_now
from ..core.errors import StorageError
from .session_store import safe_path, durable_section


class MemoryError(ValueError):
    """Auxiliary failure; never invalidates a completed coding response."""


def normalized(text):
    return re.sub(r'[\W_]+', '', unicodedata.normalize('NFKC', text).casefold())


def fingerprint(text):
    return hashlib.sha256(normalized(text).encode('utf-8')).hexdigest()


def source_key(source):
    return fingerprint(source['session_id'] + ':' + source['message_id'])


def bounded(value, limit):
    if type(value) is not str or not value.strip() or len(value) > limit:
        raise MemoryError('记忆字段为空或超出长度限制。')


def validate_source(source):
    fields(source, {'session_id','turn_id','message_id','evidence'}, {'call_id','artifact_id','path'})
    for key, kind in [('session_id','session'),('turn_id','turn'),('message_id','message')]:
        local_id(source[key], kind)
    bounded(source['evidence'], 1000)
    if 'call_id' in source: bounded(source['call_id'], 1000)
    if 'artifact_id' in source: local_id(source['artifact_id'], 'artifact')
    if 'path' in source:
        bounded(source['path'], 1000)
        path = Path(source['path'])
        if path.is_absolute() or '..' in path.parts or '.mini-claude-code' in path.parts:
            raise MemoryError('来源路径必须为项目相对路径。')


def validate_record(record):
    fields(record, {'memory_id','type','name','description','body','created_at','updated_at',
                    'revision','status','sources'}, {'superseded_by'})
    local_id(record['memory_id'], 'memory')
    if record['type'] not in {'user','feedback','project','reference'} or record['status'] not in {'active','disputed','superseded'}:
        raise MemoryError('记忆分类或状态无效。')
    for key, limit in [('name',100),('description',200),('body',2000)]: bounded(record[key], limit)
    timestamp(record['created_at']); timestamp(record['updated_at'])
    integer(record['revision'], 'revision', minimum=1)
    if type(record['sources']) is not list or not 1 <= len(record['sources']) <= 50:
        raise MemoryError('记忆来源数量无效。')
    for source in record['sources']: validate_source(source)
    if 'superseded_by' in record: local_id(record['superseded_by'], 'memory')


def empty_store():
    return {'schema_version':1,'store_revision':0,'records':[], 'tombstones':[],
            'processed_turns':[], 'changes_since_consolidation':0,'consolidated_once':False}


def validate_store(data):
    envelope(data, {'store_revision','records','tombstones','processed_turns',
                    'changes_since_consolidation','consolidated_once'})
    integer(data['store_revision'], 'store_revision')
    integer(data['changes_since_consolidation'], 'changes_since_consolidation')
    if type(data['consolidated_once']) is not bool or type(data['records']) is not list or len(data['records']) > 200:
        raise MemoryError('记忆库结构或容量无效。')
    ids = set()
    for record in data['records']:
        validate_record(record)
        if record['memory_id'] in ids: raise MemoryError('重复记忆 ID。')
        ids.add(record['memory_id'])
    if type(data['processed_turns']) is not list or type(data['tombstones']) is not list:
        raise MemoryError('记忆处理记录无效。')
    for turn in data['processed_turns']: local_id(turn, 'turn')
    if len(set(data['processed_turns'])) != len(data['processed_turns']): raise MemoryError('重复处理回合。')
    for tombstone in data['tombstones']:
        fields(tombstone, {'hash','source_keys','evidence_hashes'})
        hashes = [tombstone['hash']]
        for key in ('source_keys','evidence_hashes'):
            if type(tombstone[key]) is not list: raise MemoryError('删除抑制信息无效。')
            hashes.extend(tombstone[key])
        if any(type(item) is not str or not re.fullmatch('[0-9a-f]{64}',item) for item in hashes):
            raise MemoryError('删除抑制哈希无效。')
    return data


class MemoryStore:
    def __init__(self, workspace):
        self.workspace = Path(workspace)
        self.root = self.workspace / '.mini-claude-code' / 'memory'

    def _path(self, name):
        return safe_path(self.workspace, self.root / name)

    def snapshot(self):
        try:
            path = self._path('store.json')
            if not path.exists(): return empty_store()
            if path.stat().st_size > 8 * 1024 * 1024: raise MemoryError('记忆库超过读取上限。')
            return validate_store(json.loads(path.read_text(encoding='utf-8')))
        except (OSError, ValueError, TypeError, KeyError, StorageError, RecursionError) as exc:
            raise MemoryError('记忆库不可读取、损坏或版本不支持；保留原文件。') from exc

    @contextmanager
    def _lock(self):
        fd = None
        try:
            self._path('store.lock')
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(self._path('store.lock'), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            deadline = time.monotonic() + 0.15
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline: raise MemoryError('记忆库正忙，请稍后重试。')
                    time.sleep(0.01)
            yield
        except (OSError, StorageError) as exc:
            raise MemoryError('记忆库无法锁定或写入。') from exc
        finally:
            if fd is not None: os.close(fd)

    def transaction(self, update, *, expected_revision=None, cancellation=None):
        with self._lock():
            current = self.snapshot()
            if expected_revision is not None and current['store_revision'] != expected_revision:
                raise MemoryError('记忆版本已变化，本次整理未提交；请重试。')
            data = deepcopy(current)
            result = update(data)
            if data == current: return result
            data['store_revision'] += 1
            validate_store(data)
            payload = json.dumps(data, ensure_ascii=False, allow_nan=False).encode('utf-8')
            if len(payload) > 8 * 1024 * 1024:
                raise MemoryError('记忆库达到保存上限；原库保留。')
            name = None
            try:
                if cancellation is not None: cancellation.check()
                fd, name = tempfile.mkstemp(prefix='.pending-', dir=self.root)
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(payload)
                    stream.flush(); os.fsync(stream.fileno())
                if cancellation is not None: cancellation.check()
                with durable_section():
                    os.replace(name, self._path('store.json'))
                    fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
                    try: os.fsync(fd)
                    finally: os.close(fd)
            except (OSError, ValueError, StorageError) as exc:
                raise MemoryError('记忆更新未能可靠保存。') from exc
            finally:
                if name is not None: Path(name).unlink(missing_ok=True)
            return result

    def delete(self, memory_id):
        local_id(memory_id, 'memory')
        if not self.snapshot()['records']: raise MemoryError('没有此记忆。')
        def update(data):
            record = next((r for r in data['records'] if r['memory_id'] == memory_id), None)
            if record is None: raise MemoryError('没有此记忆。')
            data['tombstones'].append({'hash':fingerprint(record['body']),
                'source_keys':sorted({source_key(s) for s in record['sources']}),
                'evidence_hashes':sorted({fingerprint(s['evidence']) for s in record['sources']})})
            data['records'].remove(record)
        self.transaction(update)

    def add_candidates(self, candidates, turn_id, *, cancellation=None):
        local_id(turn_id, 'turn')
        def update(data):
            if turn_id in data['processed_turns']: return {'added':0,'changed':0,'capacity':False}
            added = changed = 0
            capacity = False
            for candidate in candidates:
                record = {k:deepcopy(candidate[k]) for k in ('type','name','description','body','sources')}
                explicit = candidate.get('explicit_remember', False)
                sources = record['sources']
                blocked = [t for t in data['tombstones'] if t['hash'] == fingerprint(record['body'])
                    or set(t['source_keys']) & {source_key(s) for s in sources}
                    or set(t['evidence_hashes']) & {fingerprint(s['evidence']) for s in sources}]
                if blocked and (not explicit or any(set(t['source_keys']) & {source_key(s) for s in sources} for t in blocked)):
                    continue
                existing = next((r for r in data['records'] if fingerprint(r['body']) == fingerprint(record['body'])), None)
                if existing:
                    # Never reactivate a disputed/superseded fact as a side effect of deduplication.
                    merged = existing['sources'] + [s for s in sources if s not in existing['sources']]
                    merged = merged[-50:]
                    if merged != existing['sources']:
                        existing['sources'] = merged
                        existing['revision'] += 1; existing['updated_at'] = utc_now()
                        changed += 1
                    continue
                if len(data['records']) >= 200:
                    capacity = True; continue
                now = utc_now()
                record.update(memory_id=new_id('memory'),created_at=now,updated_at=now,revision=1,status='active')
                validate_record(record)
                data['records'].append(record); added += 1; changed += 1
            data['processed_turns'].append(turn_id)
            data['changes_since_consolidation'] += changed
            return {'added':added,'changed':changed,'capacity':capacity}
        return self.transaction(update, cancellation=cancellation)

    def consolidate(self, proposals, snapshot, provided_ids, *, cancellation=None):
        if type(proposals) is not list or len(proposals) > 20: raise MemoryError('整理提议格式无效。')
        touched = set()
        for proposal in proposals:
            fields(proposal, {'action','ids'}, {'keep_id'})
            ids = proposal['ids']
            if (type(ids) is not list or not ids or any(type(item) is not str for item in ids)
                    or len(ids) != len(set(ids))
                    or not set(ids) <= set(provided_ids) or set(ids) & touched):
                raise MemoryError('整理引用未提供、重复或冲突的 ID。')
            if type(proposal['action']) is not str or proposal['action'] not in {'merge','supersede','mark_disputed'}:
                raise MemoryError('整理动作无效。')
            if proposal['action'] != 'mark_disputed' and (len(ids) < 2 or proposal.get('keep_id') not in ids):
                raise MemoryError('整理保留 ID 无效。')
            touched.update(ids)
        def update(data):
            records = {r['memory_id']:r for r in data['records']}
            for proposal in proposals:
                ids, action = proposal['ids'], proposal['action']
                if any(item not in records or records[item]['status'] != 'active' for item in ids):
                    raise MemoryError('只能整理本次提供的有效记忆。')
                if action == 'mark_disputed':
                    for item in ids: records[item]['status'] = 'disputed'
                else:
                    keep = records[proposal['keep_id']]
                    if action == 'supersede':
                        # Host requires new user evidence of an explicit ongoing correction.
                        if keep['type'] not in {'user','feedback'} or not any(
                            re.search(r'以后|今后|改为|不再|instead|from now|no longer',s['evidence'],re.I)
                            for s in keep['sources']): raise MemoryError('更新缺少明确长期纠正证据。')
                        if any(keep['created_at'] < records[item]['created_at'] for item in ids):
                            raise MemoryError('更新不能用较旧证据覆盖较新记录。')
                    if action == 'merge':
                        # Keep stable content; a model proposal never introduces a new fact body.
                        if any(keep['created_at'] > records[item]['created_at'] for item in ids):
                            raise MemoryError('合并必须保留较早的稳定 ID。')
                        merged = [s for item in ids for s in records[item]['sources']]
                        keep['sources'] = list({json.dumps(s,sort_keys=True):s for s in merged}.values())[-50:]
                    for item in ids:
                        if item != keep['memory_id']:
                            records[item]['status'] = 'superseded'; records[item]['superseded_by'] = keep['memory_id']
                for item in ids:
                    records[item]['revision'] += 1; records[item]['updated_at'] = utc_now()
            data['consolidated_once'] = True
            data['changes_since_consolidation'] = 0
            return len(proposals)
        return self.transaction(update, expected_revision=snapshot['store_revision'], cancellation=cancellation)
