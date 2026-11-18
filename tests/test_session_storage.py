import json
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import ScriptedModelClient,answer
from mini_claude_code.core.errors import StorageError
from mini_claude_code.core.loop import validate_history
from mini_claude_code.core.messages import Message,TextBlock,ToolCall,ToolResult
from mini_claude_code.storage.session_store import SessionStore,list_sessions


def interrupted(make_runtime,with_intent=False,with_result=False):
    runtime=make_runtime(ScriptedModelClient())
    store=runtime.store
    store.append_message(Message('user',[TextBlock('example')]))
    assistant=Message('assistant',[ToolCall('lost','write_file',{'path':'x','content':'data'})])
    store.append_message(assistant)
    if with_intent:store.event('tool_intent',{'assistant_message_id':assistant.message_id,'call_id':'lost','turn_id':'turn_'+'a'*32})
    if with_result:store.append_message(Message('tool',[ToolResult('lost','success','saved')]))
    sid=runtime.state.session_id;path=store.path
    runtime.close()
    return sid,path


@pytest.mark.parametrize('intent,result,status,unknown',[(False,False,'cancelled',False),(True,False,'error',True),(True,True,'success',False)])
def test_classified_recovery_is_idempotent(make_runtime,intent,result,status,unknown):
    sid,path=interrupted(make_runtime,intent,result)
    client=ScriptedModelClient()
    runtime=make_runtime(client,resume=sid)
    records=[b for m in runtime.state.messages for b in m.content if isinstance(b,ToolResult)]
    assert len(records)==1 and records[0].status==status
    assert records[0].metadata.get('execution_unknown',False)==unknown
    assert not client.requests
    validate_history(runtime.state.messages)
    runtime.close()
    again=make_runtime(ScriptedModelClient(),resume=sid)
    assert len([b for m in again.state.messages for b in m.content if isinstance(b,ToolResult)])==1
    assert not (path.parents[3]/'x').exists()


def test_torn_tail_backup_and_valid_unterminated_line(make_runtime):
    runtime=make_runtime(ScriptedModelClient(answer()))
    runtime.run('hello');sid=runtime.state.session_id;path=runtime.store.path
    runtime.close()
    log=path/'messages.jsonl';valid=log.read_bytes()
    log.write_bytes(valid+b'{"partial":')
    recovered=make_runtime(ScriptedModelClient(),resume=sid)
    assert len(recovered.store.warnings)==1 and log.read_bytes()==valid
    backups=list(path.glob('messages.jsonl.tail-*.bak'))
    assert len(backups)==1 and backups[0].read_bytes()==valid+b'{"partial":'
    recovered.close()
    log.write_bytes(valid.rstrip(b'\n'))
    resumed=make_runtime(ScriptedModelClient(),resume=sid)
    assert log.read_bytes().endswith(b'\n')
    validate_history(resumed.state.messages)


@pytest.mark.parametrize('damage',['middle','unknown_version','duplicate_message','orphan','bad_event'])
def test_invalid_complete_records_refused(make_runtime,damage):
    runtime=make_runtime(ScriptedModelClient(answer()))
    runtime.run('hello');sid=runtime.state.session_id;path=runtime.store.path
    runtime.close()
    log=path/'messages.jsonl';data=log.read_bytes()
    if damage=='middle': log.write_bytes(b'not-json\n'+data)
    elif damage=='unknown_version':
        first=json.loads(data.splitlines()[0]);first['schema_version']=99
        log.write_text(json.dumps(first)+'\n')
    elif damage=='duplicate_message':log.write_bytes(data+data.splitlines(keepends=True)[0])
    elif damage=='orphan':log.write_text(json.dumps(Message('tool',[ToolResult('none','error','orphan')]).to_dict())+'\n')
    else:(path/'events.jsonl').write_bytes(b'{"kind":')
    if damage=='bad_event':
        resumed=make_runtime(ScriptedModelClient(),resume=sid)
        assert resumed.store.warnings
    else:
        with pytest.raises(StorageError):make_runtime(ScriptedModelClient(),resume=sid)


def test_process_lock_and_release(make_runtime):
    runtime=make_runtime(ScriptedModelClient())
    command=[sys.executable,'-c',
        'import sys; from pathlib import Path; from mini_claude_code.storage.session_store import SessionStore; '
        'store=SessionStore.resume(Path(sys.argv[1]),sys.argv[2]); store.close()',
        runtime.state.workspace,runtime.state.session_id]
    blocked=subprocess.run(command,capture_output=True,text=True)
    assert blocked.returncode!=0 and '其他进程' in blocked.stderr
    runtime.close()
    assert subprocess.run(command,capture_output=True).returncode==0


def test_foreign_workspace_and_symlink_data_denied(make_runtime,config,tmp_path):
    runtime=make_runtime(ScriptedModelClient())
    sid=runtime.state.session_id;path=runtime.store.path;runtime.close()
    meta=path/'session.json';data=json.loads(meta.read_text());data['workspace']='/elsewhere';meta.write_text(json.dumps(data))
    with pytest.raises(StorageError):make_runtime(ScriptedModelClient(),resume=sid)
    other=tmp_path/'other';other.mkdir()
    (other/'.mini-claude-code').symlink_to(tmp_path/'.mini-claude-code')
    from dataclasses import replace
    with pytest.raises(StorageError):make_runtime(ScriptedModelClient(),config=replace(config,workspace=other))
    with pytest.raises(StorageError):list_sessions(other)


def test_lock_release_after_process_crash(make_runtime):
    runtime=make_runtime(ScriptedModelClient());sid=runtime.state.session_id;workspace=runtime.state.workspace
    runtime.close()
    code='import os,sys; from pathlib import Path; from mini_claude_code.storage.session_store import SessionStore; store=SessionStore.resume(Path(sys.argv[1]),sys.argv[2]); os._exit(0)'
    assert subprocess.run([sys.executable,'-c',code,workspace,sid]).returncode==0
    resumed=make_runtime(ScriptedModelClient(),resume=sid)
    assert resumed.state.session_id==sid
