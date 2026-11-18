"""Real local pipes exercise input boundaries without timing-dependent writes."""
import io
import os
import sys

import pytest

from conftest import ScriptedModelClient, answer
from mini_claude_code.console import ConsoleBroker
from mini_claude_code.runtime_events import Wakeup


@pytest.fixture
def pipe_input(monkeypatch):
    read_fd, write_fd = os.pipe()
    with os.fdopen(read_fd, 'r', encoding='utf-8') as reader, os.fdopen(write_fd, 'wb', buffering=0) as writer:
        monkeypatch.setattr(sys, 'stdin', reader)
        yield writer


@pytest.mark.parametrize('prefix,suffix,expected', [
    (b'first ', b'second\n', 'first second'),
    ('你'.encode()[:1], '你'.encode()[1:] + '好\n'.encode(), '你好'),
    (b'first ', b'second', 'first second'),
    (b'first ', b'\r\n', 'first '),
])
def test_poll_then_read_preserves_partial_input(pipe_input, prefix, suffix, expected):
    console = ConsoleBroker()
    pipe_input.write(prefix)
    assert console.poll() is None
    pipe_input.write(suffix)
    pipe_input.close()
    assert console.read() == expected
    with pytest.raises(EOFError):
        console.read()


def test_read_uses_lines_already_buffered_by_poll(pipe_input):
    pipe_input.write(b'one\ntwo\n\nlast')
    pipe_input.close()
    console = ConsoleBroker()
    assert console.poll() == 'one'
    assert console.read() == 'two'
    assert console.read() == ''
    assert console.read() == 'last'
    with pytest.raises(EOFError):
        console.poll()
    with pytest.raises(EOFError):
        console.read()


def test_read_without_fileno(monkeypatch):
    monkeypatch.setattr(sys, 'stdin', io.StringIO('one\ntwo\n'))
    console = ConsoleBroker()
    assert console.poll() is None
    assert console.read() == 'one'
    assert console.read() == 'two'
    with pytest.raises(EOFError):
        console.read()


def test_regular_file_input(tmp_path, monkeypatch):
    path = tmp_path / 'input.txt'
    path.write_text('one\ntwo\nlast')
    with path.open() as stream:
        monkeypatch.setattr(sys, 'stdin', stream)
        console = ConsoleBroker()
        assert console.poll() == 'one'
        assert console.read() == 'two'
        assert console.poll() == 'last'
        with pytest.raises(EOFError):
            console.read()


def test_wakeup_keeps_partial_input(pipe_input):
    console = ConsoleBroker()
    wakeup = Wakeup()
    try:
        pipe_input.write(b'first ')
        assert console.poll() is None
        wakeup.notify()
        assert console.read(wakeup=wakeup) is None
        pipe_input.write(b'second\n')
        assert console.read(wakeup=wakeup) == 'first second'
    finally:
        wakeup.close()


def test_minimal_cli_receives_complete_fragmented_prompt(config, pipe_input, monkeypatch):
    from mini_claude_code.cli import chat
    from mini_claude_code.config import configure_profile
    from mini_claude_code.runtime import Runtime

    client = ScriptedModelClient(answer())
    actual_poll = ConsoleBroker.poll
    first = True
    def poll(self):
        nonlocal first
        result = actual_poll(self)
        if first:
            first = False
            assert result is None
            pipe_input.write(b'second\n/exit\n')
            pipe_input.close()
        return result
    monkeypatch.setattr(ConsoleBroker, 'poll', poll)
    monkeypatch.setattr('mini_claude_code.runtime.Runtime',
                        lambda *a, **kw: Runtime(*a, **kw, client=client))
    pipe_input.write(b'first ')
    assert chat(configure_profile(config, minimal=True), stream=False) == 0
    assert len(client.requests) == 1
    assert client.requests[0].messages[-1].content[0].text == 'first second'
    assert client.closed
