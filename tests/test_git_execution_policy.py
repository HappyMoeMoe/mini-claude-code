"""Host Git must not execute repository callbacks outside Shell approval."""

import shlex

import pytest

from conftest import ScriptedModelClient, answer
from mini_claude_code.core.messages import ToolResult, ToolStatus
from test_teams_worktrees import call, init_git, invoke, response, task


def executable_marker(path, marker, *, passthrough=False):
    body = '#!/bin/sh\nprintf ran > ' + shlex.quote(str(marker)) + '\n'
    if passthrough:
        body += 'cat\n'
    path.write_text(body)
    path.chmod(0o700)
    return path.read_bytes()


@pytest.mark.parametrize('hook_location', ['default', 'absolute', 'relative', 'environment'])
def test_worktree_tool_skips_hooks_without_changing_user_config(
        config, make_runtime, monkeypatch, hook_location):
    root = config.workspace
    git = init_git(root)
    marker = root.parent / (root.name + '-hook-executed')
    hooks = root / ('.git/hooks' if hook_location == 'default' else 'custom-hooks')
    hooks.mkdir(exist_ok=True)
    hook = hooks / 'post-checkout'
    original_hook = executable_marker(hook, marker)
    if hook_location in {'absolute', 'relative'}:
        git('config', 'core.hooksPath', str(hooks) if hook_location == 'absolute' else 'custom-hooks')
    elif hook_location == 'environment':
        monkeypatch.setenv('GIT_CONFIG_COUNT', '1')
        monkeypatch.setenv('GIT_CONFIG_KEY_0', 'core.hooksPath')
        monkeypatch.setenv('GIT_CONFIG_VALUE_0', str(hooks))
    original_config = (root / '.git/config').read_bytes()
    approvals = []

    def checkout(request):
        created = next(b for m in request.messages for b in m.content
                       if isinstance(b, ToolResult) and b.metadata.get('tool_name') == 'create_task')
        return response(call('create_worktree', name='hook-safe',
                             task_id=created.metadata['target_task_id']))

    model = ScriptedModelClient(response(call('create_task', subject='isolated work')), checkout, answer())
    runtime = make_runtime(model, teams=True, interactive=False,
                           approve=lambda *args: approvals.append(args) or False)
    runtime.run('Create a task checkout without running any project scripts.')
    outputs = [b for m in runtime.state.messages for b in m.content if isinstance(b, ToolResult)]

    assert [output.status for output in outputs] == [ToolStatus.SUCCESS, ToolStatus.SUCCESS]
    assert (root / '.mini-claude-code/checkouts/hook-safe/base.txt').read_text() == 'base\n'
    assert not marker.exists(), 'worktree creation must not execute a repository Hook'
    assert not approvals
    assert hook.read_bytes() == original_hook
    assert (root / '.git/config').read_bytes() == original_config


def test_host_status_skips_fsmonitor_and_preserves_modified_file(config, make_runtime):
    root = config.workspace
    git = init_git(root)
    marker = root.parent / (root.name + '-fsmonitor-executed')
    monitor = root / '.git/audit-monitor'
    original_monitor = executable_marker(monitor, marker)
    git('config', 'core.fsmonitor', str(monitor))
    original_config = (root / '.git/config').read_bytes()
    (root / 'base.txt').write_text('changed\n')
    runtime = make_runtime(ScriptedModelClient())

    status = runtime.tasks.worktrees.git('status', '--porcelain', '--untracked-files=all')

    assert 'base.txt' in status
    assert not marker.exists(), 'host status must not execute a configured fsmonitor command'
    assert monitor.read_bytes() == original_monitor
    assert (root / '.git/config').read_bytes() == original_config


def test_empty_filter_command_does_not_block_normal_checkout(config, make_runtime):
    git = init_git(config.workspace)
    git('config', 'filter.unused.smudge', '')
    runtime = make_runtime(ScriptedModelClient(), teams=True)
    record = task(runtime)

    result = invoke(runtime, 'create_worktree', name='empty-filter', task_id=record.task_id)

    assert result.status == ToolStatus.SUCCESS, result.output
    assert (config.workspace / '.mini-claude-code/checkouts/empty-filter/base.txt').is_file()


def test_explicitly_approved_shell_keeps_users_git_hook_behavior(config, make_runtime):
    root = config.workspace
    init_git(root)
    marker = root.parent / (root.name + '-approved-hook')
    executable_marker(root / '.git/hooks/post-checkout', marker)
    approvals = []
    runtime = make_runtime(ScriptedModelClient(), interactive=True,
                           approve=lambda *args: approvals.append(args) or True)
    checkout = root / 'manual-checkout'
    command = 'git worktree add -b user-approved ' + shlex.quote(str(checkout)) + ' HEAD'

    result = invoke(runtime, 'bash', command=command)

    assert result.status == ToolStatus.SUCCESS, result.output
    assert len(approvals) == 1
    assert marker.read_text() == 'ran'
    assert (checkout / 'base.txt').read_text() == 'base\n'


@pytest.mark.parametrize('callback', ['clean', 'smudge', 'process'])
def test_external_filters_require_explicit_execution_before_creating_checkout(
        config, make_runtime, callback):
    root = config.workspace
    git = init_git(root)
    (root / '.gitattributes').write_text('base.txt filter=audit\n')
    git('add', '.gitattributes')
    git('commit', '-qm', 'declare filter before configuring executable')
    marker = root.parent / (root.name + '-filter-executed')
    program = root / '.git/audit-filter'
    original_program = executable_marker(program, marker, passthrough=callback != 'process')
    git('config', f'filter.audit.{callback}', shlex.quote(str(program)))
    original_config = (root / '.git/config').read_bytes()
    runtime = make_runtime(ScriptedModelClient(), teams=True, interactive=False)
    record = task(runtime)

    result = invoke(runtime, 'create_worktree', name='filtered', task_id=record.task_id)

    assert result.status == ToolStatus.ERROR
    assert '过滤器' in result.output
    assert not marker.exists(), 'filter commands must not replace the blocked Hook route'
    assert runtime.tasks.store.snapshot()[record.task_id].worktree is None
    assert not (root / '.mini-claude-code/checkouts/filtered').exists()
    assert not git('branch', '--list', 'mini/filtered')
    assert program.read_bytes() == original_program
    assert (root / '.git/config').read_bytes() == original_config
