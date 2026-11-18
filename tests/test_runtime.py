import json
import os
import signal
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from conftest import ScriptedModelClient, answer
from mini_claude_code.core.errors import ContractError, ModelError, StorageError
from mini_claude_code.core.loop import TurnOutcome, validate_history
from mini_claude_code.core.messages import Message, TextBlock, ToolCall, ToolResult, ToolStatus
from mini_claude_code.core.model import ModelResponse, ModelStreamEvent, StopReason
from mini_claude_code.hooks.manager import HookAction, HookDecision, HookManager


def calls(*items):
    return ModelResponse(list(items), StopReason.TOOL_CALL)


def results(runtime):
    return [b for m in runtime.state.messages for b in m.content if isinstance(b, ToolResult)]


def test_complete_code_task_and_restore(make_runtime, git_workspace):
    root = git_workspace
    (root / 'calc.py').write_text('def add(a, b):\n    return a - b\n')
    client = ScriptedModelClient(
        calls(ToolCall('read', 'read_file', {'path':'calc.py'})),
        calls(ToolCall('edit', 'edit_file', {'path':'calc.py','old_text':'a - b','new_text':'a + b'}),
              ToolCall('check', 'bash', {'command': f'{sys.executable} -c "from calc import add; assert add(2,3)==5"'})), answer())
    runtime = make_runtime(client)
    assert runtime.run('Fix addition and verify') == TurnOutcome.COMPLETED
    assert [(r.call_id,r.status) for r in results(runtime)] == [('read','success'),('edit','success'),('check','success')]
    assert results(runtime)[-1].metadata['exit_code'] == 0
    sid = runtime.state.session_id
    expected = [m.to_dict() for m in runtime.state.messages]
    runtime.close()
    restored_client = ScriptedModelClient(answer('continue'))
    resumed = make_runtime(restored_client, resume=sid)
    assert not restored_client.requests
    assert [m.to_dict() for m in resumed.state.messages] == expected
    assert resumed.run('Continue') == TurnOutcome.COMPLETED
    assert len(results(resumed)) == 3
    validate_history(resumed.state.messages)
    text = ''.join(p.read_text() for p in resumed.store.path.glob('*.json*'))
    assert 'offline-test-key' not in text


@pytest.mark.parametrize('path', ['/etc/passwd', '../outside', '.git/config', '.mini-claude-code/sessions'])
def test_path_denial(make_runtime, path):
    runtime = make_runtime(ScriptedModelClient(calls(ToolCall('deny','read_file',{'path':path})),answer()))
    runtime.run('read')
    assert results(runtime)[0].status == ToolStatus.DENIED


def test_symlink_alias_and_glob(make_runtime, tmp_path):
    (tmp_path/'outside').symlink_to('/etc')
    (tmp_path/'.git').mkdir()
    (tmp_path/'.git/config').write_text('hidden')
    (tmp_path/'alias').symlink_to(tmp_path/'.git')
    runtime = make_runtime(ScriptedModelClient(calls(
        ToolCall('symlink','write_file',{'path':'outside/never-write','content':'bad'}),
        ToolCall('metadata','read_file',{'path':'alias/config'}),
        ToolCall('glob','glob',{'pattern':'**/*'})),answer()))
    runtime.run('inspect')
    assert [r.status for r in results(runtime)[:2]] == ['denied','denied']
    assert 'passwd' not in results(runtime)[2].output and 'config' not in results(runtime)[2].output


@pytest.mark.parametrize('tool,args', [('unknown',{}),('read_file',{'path':'x','limit':True}),
    ('read_file',{'path':'x','unknown':1}),('edit_file',{'path':'x','old_text':'','new_text':'a'}),
    ('bash',{'command':'true','run_in_background':'true'})])
def test_bad_arguments_are_results(make_runtime, tool, args):
    runtime=make_runtime(ScriptedModelClient(calls(ToolCall('bad',tool,args)),answer()))
    runtime.run('inspect')
    assert results(runtime)[0].status=='error'
    validate_history(runtime.state.messages)


@pytest.mark.parametrize('interactive', [True,False])
def test_shell_approval_denied_without_execution(make_runtime,tmp_path,interactive):
    prompts=[]
    runtime=make_runtime(ScriptedModelClient(calls(ToolCall('shell','bash',{'command':'touch never'})),answer()),
                         interactive=interactive,approve=lambda *args: prompts.append(args) or False)
    runtime.run('shell')
    assert not (tmp_path/'never').exists()
    assert len(prompts)==int(interactive) and results(runtime)[0].status=='denied'


def test_hooks_copied_and_cannot_authorize(make_runtime,tmp_path):
    hooks=HookManager()
    def mutate(view):
        view['arguments']['path']='/etc/passwd'
        view['context']['interactive']=True
        return HookDecision()
    hooks.register('PreToolUse',mutate)
    runtime=make_runtime(ScriptedModelClient(calls(ToolCall('write','write_file',{'path':'safe','content':'ok'})),answer()))
    runtime.executor.hooks=hooks
    runtime.run('write')
    assert (tmp_path/'safe').read_text()=='ok'
    hooks.register('PreToolUse',lambda view: HookDecision('deny','stop'))
    runtime.client.steps=[calls(ToolCall('denied','write_file',{'path':'no','content':'bad'})),answer()]
    runtime.run('deny')
    assert not (tmp_path/'no').exists() and results(runtime)[-1].status=='denied'


def test_hook_failures_do_not_repeat_side_effects(make_runtime,tmp_path):
    warnings=[];hooks=HookManager(warnings.append);stops=[]
    def fail(view):raise ValueError('not persisted')
    hooks.register('PostToolUse',fail)
    hooks.register('Stop',lambda view: stops.append(view) or HookDecision())
    runtime=make_runtime(ScriptedModelClient(calls(ToolCall('once','write_file',{'path':'x','content':'once'})),answer()))
    runtime.executor.hooks=hooks
    runtime.run('write')
    assert (tmp_path/'x').read_text()=='once' and len(results(runtime))==1
    assert len(warnings)==1 and len(stops)==1 and stops[0]['outcome']=='completed'
    hooks.register('PreToolUse',fail)
    runtime.client.steps=[calls(ToolCall('blocked','write_file',{'path':'no','content':'bad'})),answer()]
    runtime.run('blocked')
    assert results(runtime)[-1].status=='denied' and not (tmp_path/'no').exists()


def test_user_hook_denies_before_history(make_runtime):
    runtime=make_runtime(ScriptedModelClient())
    runtime.executor.hooks.register('UserPromptSubmit',lambda view: HookDecision('deny','stop'))
    assert runtime.run('denied')==TurnOutcome.DENIED
    assert not runtime.state.messages and not runtime.client.requests


def test_retry_only_model_and_output_recovery(make_runtime,tmp_path):
    client=ScriptedModelClient(calls(ToolCall('once','write_file',{'path':'x','content':'once'})),
                              ModelError('busy',status_code=429,retryable=True),
                              ModelResponse([],StopReason.OUTPUT_TRUNCATED),answer())
    waits=[]
    runtime=make_runtime(client,wait=waits.append)
    runtime.run('write')
    assert len(client.requests)==4 and len(results(runtime))==1
    assert client.requests[-1].max_output_tokens==16000
    assert len(waits)==2 and (tmp_path/'x').read_text()=='once'


def test_fallback_per_turn_and_retry_after(make_runtime,config):
    client=ScriptedModelClient(ModelError('busy',status_code=529,retryable=True,retry_after=100),
                              ModelError('busy',status_code=529,retryable=True),answer(),answer())
    waits=[]
    runtime=make_runtime(client,config=replace(config,fallback_model='fallback'),wait=waits.append)
    runtime.run('first');runtime.run('second')
    assert [r.model for r in client.requests]==['deepseek-flash','deepseek-flash','fallback','deepseek-flash']
    assert waits[0]<=30


@pytest.mark.parametrize('status',[400,401,403])
def test_nonretryable_failures_allow_new_input(make_runtime,status):
    client=ScriptedModelClient(ModelError('failed',status_code=status),answer())
    runtime=make_runtime(client)
    with pytest.raises(ModelError):runtime.run('first')
    assert len(client.requests)==1
    assert runtime.run('second')==TurnOutcome.COMPLETED


def test_stream_interruption_never_executes_partial_calls(make_runtime,tmp_path):
    incomplete=[ModelStreamEvent('text',text='partial'),ModelError('lost',retryable=True)]
    client=ScriptedModelClient(incomplete,incomplete,incomplete)
    displayed=[]
    runtime=make_runtime(client,stream=True,wait=lambda seconds:None,display=lambda *args:displayed.append(args))
    with pytest.raises(ModelError):runtime.run('write')
    assert len(client.requests)==3 and len(runtime.state.messages)==1
    assert not results(runtime) and ('delta','partial') in displayed


def test_truncation_and_limit_have_no_unpaired_calls(make_runtime,tmp_path):
    client=ScriptedModelClient(ModelResponse([ToolCall('bad','write_file',{'path':'no','content':'bad'})],StopReason.OUTPUT_TRUNCATED),
                              ModelResponse([],StopReason.OUTPUT_TRUNCATED))
    runtime=make_runtime(client,wait=lambda seconds:None)
    with pytest.raises(ContractError):runtime.run('write')
    assert not (tmp_path/'no').exists() and len(runtime.state.messages)==1
    runtime.client.steps=[calls(ToolCall('read','read_file',{'path':'no'}))]
    runtime.loop.max_rounds=1
    assert runtime.run('read')==TurnOutcome.LIMIT_REACHED
    validate_history(runtime.state.messages)


def test_storage_intent_failure_blocks_all_handlers(make_runtime,tmp_path,monkeypatch):
    runtime=make_runtime(ScriptedModelClient(calls(ToolCall('one','write_file',{'path':'one','content':'bad'}),
                                                               ToolCall('two','write_file',{'path':'two','content':'bad'}))))
    original=runtime.store._append
    def append(name,data):
        if data.get('kind')=='tool_intent':
            runtime.store.failed=True
            raise StorageError('disk full')
        return original(name,data)
    monkeypatch.setattr(runtime.store,'_append',append)
    with pytest.raises(StorageError):runtime.run('write')
    assert not (tmp_path/'one').exists() and not (tmp_path/'two').exists()


def test_result_storage_failure_preserves_first_effect_and_stops_next(make_runtime,tmp_path,monkeypatch):
    runtime=make_runtime(ScriptedModelClient(calls(ToolCall('one','write_file',{'path':'one','content':'real'}),
                                                               ToolCall('two','write_file',{'path':'two','content':'bad'}))))
    original=runtime.store._append
    def append(name,data):
        if name=='messages.jsonl' and data.get('role')=='tool':
            runtime.store.failed=True
            raise StorageError('disk full')
        return original(name,data)
    monkeypatch.setattr(runtime.store,'_append',append)
    with pytest.raises(StorageError):runtime.run('write')
    assert (tmp_path/'one').read_text()=='real' and not (tmp_path/'two').exists()


def test_cancel_after_success_preserves_results_and_allows_next_input(make_runtime,tmp_path):
    client=ScriptedModelClient(calls(ToolCall('one','write_file',{'path':'one','content':'real'}),
                                   ToolCall('two','write_file',{'path':'two','content':'bad'})),answer())
    runtime=make_runtime(client)
    def cancel(view):
        if view['call']['call_id']=='one':runtime.cancellation.cancel()
        return HookDecision()
    runtime.executor.hooks.register('PostToolUse',cancel)
    assert runtime.run('write')==TurnOutcome.CANCELLED
    assert [r.status for r in results(runtime)]==['success','cancelled']
    assert (tmp_path/'one').read_text()=='real' and not (tmp_path/'two').exists()
    assert runtime.run('continue')==TurnOutcome.COMPLETED
    validate_history(runtime.state.messages)


def test_cancel_long_shell_returns_to_idle_and_cleans_group(make_runtime,tmp_path):
    client=ScriptedModelClient(calls(ToolCall('slow','bash',{'command':'echo $$ > shell.pid; sleep 20 & echo $! > child.pid; wait'})),answer())
    runtime=make_runtime(client)
    def cancel():
        deadline=time.monotonic()+3
        while not (tmp_path/'child.pid').exists() and time.monotonic()<deadline:time.sleep(.01)
        runtime.cancellation.cancel()
    worker=threading.Thread(target=cancel);worker.start()
    assert runtime.run('slow')==TurnOutcome.CANCELLED
    worker.join(timeout=5)
    for name in ('shell.pid','child.pid'):
        pid=int((tmp_path/name).read_text());stat=Path(f'/proc/{pid}/stat')
        assert not stat.exists() or stat.read_text().split()[2]=='Z'
    assert results(runtime)[0].metadata['execution_unknown']
    assert runtime.run('continue')==TurnOutcome.COMPLETED


def test_sigint_cancels_turn_without_exiting_process(tmp_path):
    import subprocess
    script = '''import sys
from pathlib import Path
from mini_claude_code.config import load_config
from mini_claude_code.core.messages import ToolCall,TextBlock
from mini_claude_code.core.model import ModelResponse,StopReason
from mini_claude_code.core.loop import TurnOutcome
from mini_claude_code.runtime import Runtime
class Client:
    def __init__(self):self.n=0
    def generate(self,request):
        self.n+=1
        if self.n==1:return ModelResponse([ToolCall("slow","bash",{"command":"echo $$ > shell.pid; sleep 20 & echo $! > child.pid; wait"})],StopReason.TOOL_CALL)
        return ModelResponse([TextBlock("continued")],StopReason.NORMAL)
    def cancel_active(self):pass
    def close(self):pass
root=Path(sys.argv[1])
config=load_config(workspace=root,environ={"DEEPSEEK_API_KEY":"offline"},env_file=root/"absent")
with Runtime(config,interactive=True,approve=lambda *args:True,display=lambda *args:None,client=Client(),stream=False) as runtime:
    assert runtime.run("shell")==TurnOutcome.CANCELLED
    assert runtime.run("continue")==TurnOutcome.COMPLETED
print("cancelled and continued")
'''
    process=subprocess.Popen([sys.executable,'-c',script,str(tmp_path)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        deadline=time.monotonic()+5
        while not (tmp_path/'child.pid').exists() and time.monotonic()<deadline:
            assert process.poll() is None
            time.sleep(.02)
        assert (tmp_path/'child.pid').exists()
        process.send_signal(signal.SIGINT)
        stdout,stderr=process.communicate(timeout=5)
        assert process.returncode==0,(stdout,stderr)
        assert 'cancelled and continued' in stdout
        for name in ('shell.pid','child.pid'):
            pid=int((tmp_path/name).read_text());stat=Path(f'/proc/{pid}/stat')
            assert not stat.exists() or stat.read_text().split()[2]=='Z'
    finally:
        if process.poll() is None:
            process.kill();process.wait()
