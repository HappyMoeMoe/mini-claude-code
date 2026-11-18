"""Non-regular workspace inputs must fail promptly, including during discovery."""
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest


@pytest.mark.parametrize('operation', ['read_file', 'edit_file', 'skills', 'swap_fifo'])
def test_fifo_rejected_without_waiting_for_a_writer(tmp_path, operation):
    pipe = tmp_path / ('skills/demo/SKILL.md' if operation == 'skills' else 'input.pipe')
    pipe.parent.mkdir(parents=True, exist_ok=True)
    if operation == 'swap_fifo':
        pipe.write_text('initial regular file')
    else:
        os.mkfifo(pipe)
    script = textwrap.dedent('''
        import os, sys, threading
        from pathlib import Path
        from types import SimpleNamespace
        from mini_claude_code.config import load_config, configure_profile
        from mini_claude_code.runtime import Runtime
        from mini_claude_code.core.cancellation import CancellationToken
        from mini_claude_code.core.messages import ToolCall, ToolStatus
        root, operation = Path(sys.argv[1]), sys.argv[2]
        cfg = configure_profile(load_config(workspace=root,
            environ={'DEEPSEEK_API_KEY': 'fake-offline-key'}, env_file=root/'absent.env'), minimal=True)
        runtime = Runtime(cfg, interactive=False, client=SimpleNamespace(close=lambda: None))
        original_open = os.open
        if operation == 'swap_fifo':
            def swapping_open(path, *args, **kwargs):
                if Path(path) == root/'input.pipe':
                    Path(path).unlink()
                    os.mkfifo(path)
                return original_open(path, *args, **kwargs)
            os.open = swapping_open
        try:
            token = CancellationToken()
            timer = threading.Timer(.1, token.cancel)
            timer.daemon = True
            timer.start()
            if operation == 'skills':
                catalog = runtime.skills.refresh()
                assert not catalog.entries and catalog.warnings
            else:
                args = {'path': 'input.pipe'}
                if operation == 'edit_file':
                    args.update(old_text='before', new_text='after')
                result = runtime.executor.execute(ToolCall('fifo', 'read_file' if operation == 'swap_fifo' else operation, args),
                                                  runtime.context, cancellation=token)
                assert result.status == ToolStatus.ERROR, result
                assert 'regular' in result.output.lower(), result.output
            timer.cancel()
            result = runtime.executor.execute(ToolCall('next', 'write_file',
                {'path': 'next.txt', 'content': 'still usable'}), runtime.context)
            assert result.status == ToolStatus.SUCCESS, result
        finally:
            os.open = original_open
            runtime.close()
    ''')
    try:
        completed = subprocess.run([sys.executable, '-c', script, str(tmp_path), operation],
                                   capture_output=True, text=True, timeout=3)
    except subprocess.TimeoutExpired:
        pytest.fail(f'{operation} blocked on a FIFO despite cancellation; isolated child terminated')
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert pipe.is_fifo()
    assert (tmp_path / 'next.txt').read_text() == 'still usable'


def test_cancelled_read_never_opens_fifo(tmp_path, make_runtime):
    from conftest import ScriptedModelClient
    from mini_claude_code.core.cancellation import CancellationToken
    from mini_claude_code.core.messages import ToolCall, ToolStatus
    os.mkfifo(tmp_path / 'input.pipe')
    runtime = make_runtime(ScriptedModelClient())
    token = CancellationToken()
    token.cancel()
    result = runtime.executor.execute(ToolCall('cancelled', 'read_file', {'path': 'input.pipe'}),
                                      runtime.context, cancellation=token)
    assert result.status == ToolStatus.CANCELLED
    assert not result.metadata['executed']


def test_regular_utf8_symlink_read_and_edit_keep_newlines(tmp_path, make_runtime):
    from conftest import ScriptedModelClient
    from mini_claude_code.core.messages import ToolCall, ToolStatus
    original = tmp_path / 'original.txt'
    original.write_bytes('你好\r\n世界\r\n'.encode())
    (tmp_path / 'alias.txt').symlink_to(original)
    runtime = make_runtime(ScriptedModelClient())
    read = runtime.executor.execute(ToolCall('read', 'read_file',
        {'path': 'alias.txt', 'offset': 1, 'limit': 1}), runtime.context)
    assert read.status == ToolStatus.SUCCESS and '2: 世界' in read.output
    edit = runtime.executor.execute(ToolCall('edit', 'edit_file',
        {'path': 'alias.txt', 'old_text': '世界', 'new_text': '朋友'}), runtime.context)
    assert edit.status == ToolStatus.SUCCESS
    assert original.read_bytes() == '你好\r\n朋友\r\n'.encode()
    assert (tmp_path / 'alias.txt').is_symlink()
