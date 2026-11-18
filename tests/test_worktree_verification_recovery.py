"""Unavailable Git verification must not rewrite a fully committed checkout."""
from pathlib import Path
from dataclasses import replace
import shutil
import subprocess

import pytest

from conftest import ScriptedModelClient
from test_teams_worktrees import init_git, task
from mini_claude_code.tasks.models import TaskError
from mini_claude_code.core.errors import StorageError
from mini_claude_code.storage.event_store import atomic_json
from mini_claude_code.worktrees.service import EXTERNAL_FILTERS, GitVerificationUnavailable


def ready(config, make_runtime):
    git = init_git(config.workspace)
    runtime = make_runtime(ScriptedModelClient())
    record = task(runtime)
    row = runtime.tasks.worktrees.create('retained', record.task_id, 'HEAD', runtime.tasks.source('host'))
    return git, runtime, runtime.tasks.store.snapshot()[record.task_id], row


def test_filter_refusal_on_resume_preserves_ready_and_later_claims_without_repair(config, make_runtime):
    git, runtime, before, row = ready(config, make_runtime)
    root = Path(row['path'])
    (root / 'base.txt').write_text('retained dirty change\n')
    (root / 'unfinished.txt').write_text('retained untracked file\n')
    operation = runtime.tasks.worktrees._path(row['operation_id'] + '.json')
    original_operation = operation.read_bytes()
    session_id = runtime.state.session_id
    runtime.close()
    git('config', 'filter.audit.clean', 'cat')
    notices = []
    try:
        restored = make_runtime(ScriptedModelClient(), resume=session_id,
                                display=lambda kind, value: notices.append(str(value)))
        assert restored.tasks.store.snapshot()[before.task_id] == before
        assert operation.read_bytes() == original_operation
        assert any('retained' in value and '校验' in value for value in notices)
        with pytest.raises(TaskError, match='过滤器'):
            restored.tasks.claim_host(before.task_id)
        assert restored.tasks.assignment is None and restored.tasks.lease is None
        assert restored.tasks.store.snapshot()[before.task_id] == before
    finally:
        git('config', '--unset-all', 'filter.audit.clean')
    assert restored.tasks.worktrees.recover() == []
    assert restored.tasks.store.snapshot()[before.task_id] == before
    assert operation.read_bytes() == original_operation
    restored.tasks.claim_host(before.task_id)
    assert restored.context.cwd == row['path']
    assert restored.tasks.assignment.owner == restored.tasks.store.snapshot()[before.task_id].owner
    assert (root / 'base.txt').read_text() == 'retained dirty change\n'
    assert (root / 'unfinished.txt').read_text() == 'retained untracked file\n'


@pytest.mark.parametrize('failure', [
    'timeout', 'missing_git', 'permission', 'config_failure',
    'malformed_config', 'command_failure', 'encoding',
])
def test_unavailable_git_preserves_authorities_and_blocks_claim_until_retry(
        config, make_runtime, monkeypatch, failure):
    _, runtime, before, row = ready(config, make_runtime)
    service = runtime.tasks.worktrees
    operation = service._path(row['operation_id'] + '.json')
    task_path = runtime.tasks.store._path(before.task_id + '.json')
    original_operation, original_task = operation.read_bytes(), task_path.read_bytes()
    original = subprocess.run
    def unavailable(command, *args, **kwargs):
        probe = EXTERNAL_FILTERS in command
        if failure == 'timeout': raise subprocess.TimeoutExpired(command, 20)
        if failure == 'missing_git': raise FileNotFoundError('fixture Git unavailable')
        if failure == 'permission': raise PermissionError('fixture Git unavailable')
        if failure == 'encoding': raise UnicodeDecodeError('utf-8', b'\xff', 0, 1, 'fixture')
        if failure == 'config_failure' and probe:
            return subprocess.CompletedProcess(command, 2, '', 'fixture config unreadable')
        if failure == 'malformed_config' and probe:
            return subprocess.CompletedProcess(command, 0, 'invalid-config-entry\0', '')
        if failure == 'command_failure' and not probe:
            return subprocess.CompletedProcess(command, 128, '', 'fixture Git failed')
        return original(command, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(subprocess, 'run', unavailable)
        warnings = service.recover()
        assert len(warnings) == 1 and 'ready' in warnings[0] and 'retained' in warnings[0]
        assert operation.read_bytes() == original_operation and task_path.read_bytes() == original_task
        with pytest.raises(GitVerificationUnavailable): runtime.tasks.claim_host(before.task_id)
        assert runtime.tasks.assignment is None and runtime.tasks.lease is None
        assert operation.read_bytes() == original_operation and task_path.read_bytes() == original_task
    assert service.recover() == []
    assert operation.read_bytes() == original_operation and task_path.read_bytes() == original_task
    runtime.tasks.claim_host(before.task_id)
    assert runtime.context.cwd == row['path']


@pytest.mark.parametrize('damage', ['branch_changed', 'directory_missing'])
def test_proven_identity_damage_still_downgrades_and_blocks_work(config, make_runtime, damage):
    git, runtime, before, row = ready(config, make_runtime)
    if damage == 'branch_changed':
        git('branch', 'different', 'HEAD')
        git('-C', row['path'], 'symbolic-ref', 'HEAD', 'refs/heads/different')
    else:
        shutil.rmtree(row['path'])
    assert runtime.tasks.worktrees.recover() == []
    current = runtime.tasks.store.snapshot()[before.task_id]
    assert current.worktree['status'] in {'partial', 'failed'}
    with pytest.raises(TaskError, match='尚未 ready'):
        runtime.tasks.claim_host(before.task_id)
    assert runtime.tasks.assignment is None


def test_incomplete_transaction_is_not_promoted_after_filter_is_removed(config, make_runtime):
    git, runtime, before, row = ready(config, make_runtime)
    service = runtime.tasks.worktrees
    operation = service._path(row['operation_id'] + '.json')
    atomic_json(config.workspace, operation, {**row, 'status': 'preparing'})
    with runtime.tasks.store._locked():
        runtime.tasks.store._write(replace(before, worktree={**before.worktree, 'status': 'preparing'},
                                          revision=before.revision + 1))
    git('config', 'filter.audit.clean', 'cat')
    try: service.recover()
    finally: git('config', '--unset-all', 'filter.audit.clean')
    partial = runtime.tasks.store.snapshot()[before.task_id]
    assert partial.worktree['status'] == 'partial'
    original_operation = operation.read_bytes()
    assert service.recover() == []
    assert runtime.tasks.store.snapshot()[before.task_id] == partial
    assert operation.read_bytes() == original_operation
    with pytest.raises(TaskError, match='尚未 ready'):
        runtime.tasks.claim_host(before.task_id)


def test_corrupt_operation_record_still_fails_closed(config, make_runtime):
    _, runtime, before, row = ready(config, make_runtime)
    service = runtime.tasks.worktrees
    operation = service._path(row['operation_id'] + '.json')
    saved = operation.read_bytes()
    operation.write_text('{broken')
    try:
        with pytest.raises(StorageError): service.recover()
        assert operation.read_text() == '{broken'
        assert runtime.tasks.store.snapshot()[before.task_id] == before
    finally:
        operation.write_bytes(saved)
