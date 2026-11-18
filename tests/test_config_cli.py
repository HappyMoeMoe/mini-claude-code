import contextlib
import io
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import ScriptedModelClient,answer
from mini_claude_code.cli import main
from mini_claude_code.config import load_config
from mini_claude_code.core.errors import ConfigurationError,ContractError
from mini_claude_code.core.messages import Message,TextBlock,ToolCall
from mini_claude_code.runtime import Runtime


def test_dotenv_precedence_and_no_environment_mutation(tmp_path):
    application=tmp_path/'app.env'
    application.write_text('DEEPSEEK_API_KEY=file-key\nMODEL_ID=file-model\nDEEPSEEK_BASE_URL=https://file.invalid/anthropic\n')
    before=dict(os.environ)
    config=load_config(workspace=tmp_path,environ={},env_file=application)
    assert config.model=='file-model' and config.api_key=='file-key'
    config=load_config(workspace=tmp_path,environ={'MODEL_ID':'env-model'},env_file=application)
    assert config.model=='env-model'
    config=load_config(workspace=tmp_path,model='cli-model',environ={'MODEL_ID':'env-model'},env_file=application)
    assert config.model=='cli-model' and dict(os.environ)==before
    assert 'file-key' not in repr(config)
    with pytest.raises(ConfigurationError):load_config(workspace=tmp_path/'missing',environ={},env_file=application)
    with pytest.raises(ConfigurationError):load_config(workspace=tmp_path,max_output_tokens=10,max_recovery_output_tokens=9)


def test_help_version_and_management_without_key(tmp_path,monkeypatch,capsys):
    monkeypatch.setenv('DEEPSEEK_API_KEY','')
    with pytest.raises(SystemExit) as caught:main(['--help'])
    assert caught.value.code==0
    with pytest.raises(SystemExit) as caught:main(['--version'])
    assert caught.value.code==0
    assert main(['--workspace',str(tmp_path),'sessions','list'])==0
    assert not (tmp_path/'.mini-claude-code').exists()
    assert main(['--workspace',str(tmp_path),'chat'])==2


def test_imports_have_no_runtime_effects(tmp_path):
    code='''import socket,threading,signal
socket.socket.connect=lambda *args: (_ for _ in ()).throw(AssertionError("network"))
before=threading.enumerate(); handler=signal.getsignal(signal.SIGTERM)
import mini_claude_code.cli,mini_claude_code.runtime,mini_claude_code.providers.deepseek
assert threading.enumerate()==before and signal.getsignal(signal.SIGTERM)==handler
'''
    assert subprocess.run([sys.executable,'-c',code],cwd=tmp_path).returncode==0
    assert not list(tmp_path.iterdir())


def test_config_check_redacts_url_and_key(tmp_path,monkeypatch,capsys):
    monkeypatch.setenv('DEEPSEEK_API_KEY','hidden-key')
    assert main(['--workspace',str(tmp_path),'--base-url','https://user:password@example.invalid/anthropic?token=hidden#fragment','check'])==0
    output=capsys.readouterr().out
    assert all(value not in output for value in ('hidden-key','password','token=','fragment'))
    assert not (tmp_path/'.mini-claude-code').exists()


def test_cli_model_failure_then_next_input(make_runtime,config,monkeypatch):
    from mini_claude_code.core.errors import ModelError
    from mini_claude_code import cli
    client=ScriptedModelClient(ModelError('offline failure'),answer())
    actual=Runtime
    monkeypatch.setattr('mini_claude_code.runtime.Runtime',lambda *args,**kwargs: actual(*args,**kwargs,client=client))
    inputs=iter(['first','second','/exit'])
    monkeypatch.setattr('builtins.input',lambda *args:next(inputs))
    assert cli.chat(config,stream=False)==0
    assert len(client.requests)==2 and client.closed


def test_cli_execution_cancel_then_continue(config,monkeypatch):
    from mini_claude_code import cli
    client=ScriptedModelClient(KeyboardInterrupt(),answer())
    actual=Runtime
    monkeypatch.setattr('mini_claude_code.runtime.Runtime',lambda *args,**kwargs: actual(*args,**kwargs,client=client))
    inputs=iter(['first','second','/exit'])
    monkeypatch.setattr('builtins.input',lambda *args:next(inputs))
    assert cli.chat(config,stream=False)==0
    assert len(client.requests)==2 and client.closed


def test_cli_idle_interrupt_closes_session(config,monkeypatch):
    from mini_claude_code import cli
    client=ScriptedModelClient()
    actual=Runtime
    monkeypatch.setattr('mini_claude_code.runtime.Runtime',lambda *args,**kwargs: actual(*args,**kwargs,client=client))
    def interrupt(*args):raise KeyboardInterrupt()
    monkeypatch.setattr('builtins.input',interrupt)
    assert cli.chat(config,stream=False)==130 and client.closed


@pytest.mark.parametrize('arguments',[{'bad':object()},{'number':float('nan')},{1:'wrong-key'}])
def test_non_json_tool_arguments_rejected(arguments):
    with pytest.raises(ContractError):ToolCall('id','test',arguments)


def test_message_unknown_version_role_and_roundtrip():
    msg=Message('assistant',[TextBlock('before'),ToolCall('one','read_file',{'path':'x'}),TextBlock('after')])
    import json
    assert Message.from_dict(json.loads(json.dumps(msg.to_dict())))==msg
    with pytest.raises(ContractError):Message.from_dict(msg.to_dict()|{'schema_version':2})
    with pytest.raises(ContractError):Message('invalid',[TextBlock('no')])
