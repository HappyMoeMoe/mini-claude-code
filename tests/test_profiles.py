"""Mechanism switches remove both visibility and execution, retaining safety guards."""
from dataclasses import replace
from pathlib import Path
import pytest
from conftest import ScriptedModelClient,answer
from mini_claude_code.config import configure_profile
from mini_claude_code.core.errors import ConfigurationError
from mini_claude_code.core.messages import ToolCall,ToolResult,ToolStatus
from mini_claude_code.core.model import ModelResponse,StopReason
from mini_claude_code.runtime import Runtime
from mini_claude_code.cli import main


def test_minimal_runs_same_loop_saves_resumes_and_blocks_disabled(config,make_runtime):
    cfg=configure_profile(replace(config,memory_enabled=True),minimal=True)
    client=ScriptedModelClient(ModelResponse([ToolCall('write','write_file',{'path':'hello.txt','content':'hello'}),ToolCall('blocked','schedule_cron',{'cron':'* * * * *','prompt':'x'})],StopReason.TOOL_CALL),answer())
    r=make_runtime(client,config=cfg);r.run('write hello')
    names={s.name for s in client.requests[0].tools}
    assert names=={'read_file','write_file','edit_file','glob','bash','read_tool_output','compact'}
    assert not r.memory.config.memory_enabled and r.events is None and r.task_runner is None
    assert not r.registry.get('bash').spec.input_schema['properties'].get('run_in_background')
    assert (config.workspace/'hello.txt').read_text()=='hello'
    result=[b for m in r.state.messages for b in m.content if isinstance(b,ToolResult)]
    assert result[-1].status!=ToolStatus.SUCCESS
    system=client.requests[0].system
    assert 'Session Todo:' not in system and 'Skill catalog (' not in system and 'Delegate bounded' not in system
    sid=r.state.session_id;r.close()
    fresh=make_runtime(ScriptedModelClient(answer()),config=cfg,resume=sid);fresh.run('continue')
    assert fresh.events is None and not (config.data_dir/'scheduler').exists() and not (config.data_dir/'memory').exists()


@pytest.mark.parametrize('feature,absent',[
    ('skills',{'load_skill'}),('subagents',{'task'}),
    ('tasks',{'create_task','claim_task','complete_task','list_tasks','get_task','update_task'}),
    ('background',set()),('scheduler',{'schedule_cron','list_crons','cancel_cron'}),
])
def test_individual_switches_remove_tools_without_stopping_basics(config,make_runtime,feature,absent):
    cfg=configure_profile(config,disabled=[feature]);r=make_runtime(ScriptedModelClient(answer()),config=cfg)
    with r:
        r.run('hello');names={s.name for s in r.registry.specs()}
        assert {'read_file','write_file','bash'}<=names and not names&absent
        for name in absent:assert r.executor.execute(ToolCall('x',name,{}),r.context).status!=ToolStatus.SUCCESS
        if feature=='scheduler':assert r.events.cron.thread is None and r.events.cron.leader is None
        if feature=='background':
            assert not r.executor.allow_background and r.shell.background_handler is None
            assert 'run_in_background' not in r.registry.get('bash').spec.input_schema['properties']


@pytest.mark.parametrize('options',[{'minimal':True,'memory':'on'},{'minimal':True,'teams':True},{'minimal':True,'mcp_config':'/tmp/config.json'},{'disabled':['tasks'],'teams':True}])
def test_invalid_profile_combinations(config,options):
    with pytest.raises(ConfigurationError):configure_profile(config,**options)


@pytest.mark.parametrize('args',[['--minimal','--memory','on'],['--minimal','--teams'],['--disable','other'],['--disable','tasks','--teams']])
def test_cli_rejects_conflicts_before_model(tmp_path,args):
    assert main(['--workspace',str(tmp_path),'chat',*args])==2
    assert not (tmp_path/'.mini-claude-code').exists()


def test_tasks_disabled_retains_directory_lease_guard(config,make_runtime):
    from test_teams_worktrees import task,invoke
    owner=make_runtime(ScriptedModelClient());t=task(owner);invoke(owner,'claim_task',task_id=t.task_id)
    disabled=make_runtime(ScriptedModelClient(),config=configure_profile(config,disabled=['tasks']))
    out=disabled.executor.execute(ToolCall('w','write_file',{'path':'conflict.txt','content':'no'}),disabled.context)
    assert out.status!=ToolStatus.SUCCESS and not (config.workspace/'conflict.txt').exists()
