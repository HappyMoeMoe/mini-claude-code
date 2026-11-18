"""Isolated workspaces and offline fixtures; real_api tests use live credentials."""

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import socket
import subprocess

import pytest

from mini_claude_code.config import load_config
from mini_claude_code.core.messages import TextBlock
from mini_claude_code.core.model import ModelResponse, ModelStreamEvent, StopReason
from mini_claude_code.runtime import Runtime


class ScriptedModelClient:
    def __init__(self, *steps):
        self.steps = list(steps)
        self.requests = []
        self.closed = False

    def _next(self, request):
        self.requests.append(deepcopy(request))
        if not self.steps:
            raise AssertionError("Unexpected model request")
        step = self.steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step(request) if callable(step) else step

    def generate(self, request):
        return self._next(request)

    def stream(self, request):
        step = self._next(request)
        if isinstance(step, ModelResponse):
            for block in step.content:
                if isinstance(block, TextBlock):
                    yield ModelStreamEvent("text", text=block.text)
            yield ModelStreamEvent("final", response=step)
        else:
            for item in step:
                if isinstance(item, BaseException):
                    raise item
                yield item

    def cancel_active(self):
        pass

    def close(self):
        self.closed = True


def answer(text="done"):
    return ModelResponse([TextBlock(text)], StopReason.NORMAL)


@pytest.fixture(autouse=True)
def offline(request, monkeypatch):
    if request.node.get_closest_marker('real_api') is not None:
        return
    def fail(*args, **kwargs):
        raise AssertionError("Real network is forbidden in offline regression tests")
    monkeypatch.setattr(socket.socket, "connect", fail)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-test-key")
    monkeypatch.delenv("FALLBACK_MODEL_ID", raising=False)


@pytest.fixture
def config(tmp_path):
    # Main-loop tests exclude optional memory requests.
    # Memory tests enable the production default explicitly.
    return replace(load_config(workspace=tmp_path, environ={"DEEPSEEK_API_KEY": "offline-test-key"},
                               env_file=tmp_path / "missing.env"), memory_enabled=False)


@pytest.fixture
def make_runtime(config):
    runtimes = []
    def make(client, **kwargs):
        runtime = Runtime(kwargs.pop("config", config), interactive=kwargs.pop("interactive", True),
                          approve=kwargs.pop("approve", lambda *args: True), display=kwargs.pop("display", lambda *args: None),
                          client=client, stream=kwargs.pop("stream", False), **kwargs)
        runtimes.append(runtime)
        return runtime
    yield make
    for runtime in reversed(runtimes):
        runtime.close()


@pytest.fixture
def git_workspace(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    return tmp_path
