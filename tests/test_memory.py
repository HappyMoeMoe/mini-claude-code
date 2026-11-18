"""Memory behavior across real runtimes and atomic workspace stores."""

from copy import deepcopy
from dataclasses import replace
import fcntl
import json
import os

import pytest

from conftest import ScriptedModelClient, answer
from mini_claude_code import cli
from mini_claude_code.config import load_config
from mini_claude_code.context.memory import MemoryService, rank
from mini_claude_code.core._validation import new_id
from mini_claude_code.core.cancellation import CancellationToken, Cancelled
from mini_claude_code.core.errors import ModelError
from mini_claude_code.core.loop import TurnOutcome
from mini_claude_code.core.messages import Message, MessageRole, TextBlock, ToolCall, ToolResult
from mini_claude_code.core.model import ModelResponse, StopReason
from mini_claude_code.storage.memory_store import MemoryStore, MemoryError


PREFERENCE = '请记住：这个项目以后统一使用 pytest 编写 Python 测试。'


def candidate(message_id, quote=PREFERENCE, body='这个项目统一使用 pytest 编写 Python 测试。', **changes):
    item=dict(scope='persistent', type='user', name='Python 测试约定', description='Python pytest 测试约定',
              body=body, sources=[{'message_id':message_id,'evidence':quote}])
    item.update(changes)
    return item


def saved_candidate(body='这个项目统一使用 pytest 编写 Python 测试。', **changes):
    source=dict(session_id=new_id('session'),turn_id=new_id('turn'),message_id=new_id('message'),evidence=PREFERENCE)
    item=dict(type='user',name='Python 测试约定',description='Python pytest 测试约定',body=body,sources=[source])
    item.update(changes)
    return item


def seed(store, *items):
    store.add_candidates(list(items) or [saved_candidate()],new_id('turn'))
    return store.snapshot()['records']


class MemoryModel(ScriptedModelClient):
    def __init__(self,*steps,selector=None,extractor=None,organizer=None):
        super().__init__(*steps)
        self.selector=selector
        self.extractor=extractor
        self.organizer=organizer
        self.aux=[]
    def generate(self,request):
        if request.tools: return super().generate(request)
        data=json.loads(request.messages[0].content[0].text)
        purpose='select' if 'directory' in data else 'extract' if 'source_records' in data else 'consolidate'
        self.aux.append((purpose,deepcopy(request),data))
        handler=getattr(self,{'select':'selector','extract':'extractor','consolidate':'organizer'}[purpose])
        if isinstance(handler,BaseException): raise handler
        value=handler(data) if callable(handler) else ([] if handler is None else handler)
        return answer(json.dumps(value,ensure_ascii=False))


def extract_preference(data):
    user=next(m for m in data['source_records'] if m['role']=='user')
    return [candidate(user['message_id'],user['content'][0]['text'])]


def test_default_and_cross_session_recall(make_runtime,config):
    assert load_config(workspace=config.workspace,environ={},env_file=config.workspace/'none').memory_enabled
    cfg=replace(config,memory_enabled=True)
    first=MemoryModel(answer(),extractor=extract_preference)
    rt=make_runtime(first,config=cfg)
    assert rt.run(PREFERENCE)==TurnOutcome.COMPLETED
    records=rt.memory.store.snapshot()['records']
    assert len(records)==1 and records[0]['sources'][0]['message_id']==rt.state.messages[0].message_id
    assert [name for name,_,_ in first.aux]==['extract']
    second=MemoryModel(answer(),selector=lambda d:[d['directory'][0]['memory_id']])
    other=make_runtime(second,config=cfg)
    other.run('查看 Python 测试约定')
    assert records[0]['body'] in second.requests[0].system
    assert 'CURRENT user requests and host permissions' in second.requests[0].system
    assert other.state.messages[0].content[0].text=='查看 Python 测试约定'
    assert other.state.session_id!=rt.state.session_id
    unrelated=MemoryModel(answer(),selector=[])
    make_runtime(unrelated,config=cfg).run('计算 2+2')
    assert records[0]['memory_id'] not in unrelated.requests[0].system


def test_off_has_no_memory_reads_writes_or_aux(make_runtime,config,monkeypatch):
    def forbidden(*a,**k): raise AssertionError('off must not inspect memory')
    monkeypatch.setattr(MemoryStore,'snapshot',forbidden)
    client=MemoryModel(answer(),extractor=extract_preference)
    runtime=make_runtime(client)
    runtime.run(PREFERENCE)
    assert not client.aux and not runtime.memory.selected
    assert 'Automatic workspace memory is off' in client.requests[0].system
    assert not (config.data_dir/'memory').exists()


def test_management_without_key_and_off_still_manages(config,monkeypatch,capsys):
    store=MemoryStore(config.workspace);record=seed(store)[0]
    monkeypatch.setenv('DEEPSEEK_API_KEY','')
    base=['--workspace',str(config.workspace),'--memory','off','memory']
    assert cli.main(base+['list'])==0
    assert record['memory_id'] in capsys.readouterr().out
    assert cli.main(base+['show',record['memory_id']])==0
    shown=json.loads(capsys.readouterr().out); assert shown['sources'] and shown['revision']==1
    assert cli.main(base+['consolidate'])==2
    capsys.readouterr()
    assert cli.main(base+['delete',record['memory_id']])==0
    data=store.snapshot(); assert not data['records'] and data['tombstones']
    assert 'pytest' not in json.dumps(data['tombstones'],ensure_ascii=False)


@pytest.mark.parametrize('changes',[
    {'scope':'current_task'},
    {'body':'仅本次使用 pytest'},
    {'body':'API_KEY=super-secret-value'},
    {'body':'sk-12345678901234567890'},
    {'memory_id':'memory_'+'0'*32},
    {'sources':[{'message_id':new_id('message'),'evidence':PREFERENCE}]},
    {'sources':[{'message_id':'bad','evidence':'伪造来源'}]},
    {'status':'active'},
    {'body':'x'*2001},
    {'sources':[{'message_id':'bad','evidence':'这个项目以后偏好哪个测试框架？'}]},
])
def test_candidate_rejects_unsafe_or_forged(make_runtime,config,changes):
    def extract(data):
        msg=next(m for m in data['source_records'] if m['role']=='user')
        return [candidate(msg['message_id'],**changes)]
    runtime=make_runtime(MemoryModel(answer(),extractor=extract),config=replace(config,memory_enabled=True))
    runtime.run(PREFERENCE)
    assert runtime.memory.store.snapshot()['records']==[]


@pytest.mark.parametrize('role,metadata',[
    (MessageRole.ASSISTANT,{}),
    (MessageRole.TOOL,{'tool_name':'task'}),
    (MessageRole.TOOL,{'tool_name':'todo_write'}),
    (MessageRole.TOOL,{'tool_name':'read_file','execution_unknown':True}),
])
def test_assistant_todo_and_subagent_reports_not_project_proof(config,role,metadata):
    service=MemoryService(config,MemoryModel())
    quote='项目采用 Python 编写测试。'
    content=[TextBlock(quote)] if role==MessageRole.ASSISTANT else [ToolResult('call','success',quote,metadata)]
    message=Message(role,content)
    with pytest.raises(MemoryError):
        service._candidate(candidate(message.message_id,quote=quote,type='project'),{message.message_id:message},
                           new_id('session'),new_id('turn'))


def test_selection_fallback_and_once_per_tool_loop(make_runtime,config):
    store=MemoryStore(config.workspace);record=seed(store)[0]
    client=MemoryModel(ModelResponse([ToolCall('glob','glob',{'pattern':'*.py'})],StopReason.TOOL_CALL),answer(),
                       selector=ModelError('selection failed'))
    runtime=make_runtime(client,config=replace(config,memory_enabled=True))
    runtime.run('Python pytest 测试约定')
    assert len([x for x in client.aux if x[0]=='select'])==1
    assert all(record['memory_id'] in request.system for request in client.requests)
    assert rank([record],'毫不相关的中文查询')==[]
    assert rank([record],'测试约定')==[record]


def test_deleted_or_changed_selection_removed_before_next_request(make_runtime,config):
    store=MemoryStore(config.workspace);record=seed(store)[0]
    client=MemoryModel(selector=lambda d:[record['memory_id']])
    runtime=make_runtime(client,config=replace(config,memory_enabled=True))
    def first(request):
        assert record['memory_id'] in request.system
        store.delete(record['memory_id'])
        return ModelResponse([ToolCall('glob','glob',{'pattern':'*.py'})],StopReason.TOOL_CALL)
    client.steps=[first,answer()]
    runtime.run('Python 测试约定')
    assert record['memory_id'] not in client.requests[-1].system
    assert len([x for x in client.aux if x[0]=='select'])==1


def test_delete_suppresses_same_source_rewording_and_allows_new_explicit_evidence(config):
    store=MemoryStore(config.workspace)
    item=saved_candidate();record=seed(store,item)[0]
    store.delete(record['memory_id'])
    old=deepcopy(item);old['body']='改写措辞后的 pytest 约定'
    assert store.add_candidates([old],new_id('turn'))['added']==0
    assert store.add_candidates([item],new_id('turn'))['added']==0
    fresh=saved_candidate();fresh['explicit_remember']=True
    assert store.add_candidates([fresh],new_id('turn'))['added']==1


def test_dedup_and_turn_idempotency(config):
    store=MemoryStore(config.workspace);item=saved_candidate();turn=new_id('turn')
    assert store.add_candidates([item],turn)['added']==1
    before=store.snapshot()
    assert store.add_candidates([item],turn)['changed']==0 and store.snapshot()==before
    extra=saved_candidate();store.add_candidates([extra],new_id('turn'))
    records=store.snapshot()['records']
    assert len(records)==1 and len(records[0]['sources'])==2 and records[0]['revision']==2


def test_stale_revision_does_not_overwrite_concurrent_update(config):
    store=MemoryStore(config.workspace);records=seed(store,saved_candidate('事实甲'),saved_candidate('事实乙'))
    old=store.snapshot()
    seed(MemoryStore(config.workspace),saved_candidate('新并发事实'))
    before=store.snapshot()
    with pytest.raises(MemoryError):
        store.consolidate([{'action':'mark_disputed','ids':[r['memory_id'] for r in records]}],old,
                          [r['memory_id'] for r in records])
    assert store.snapshot()==before


def test_dispute_and_unknown_proposal_are_atomic(config):
    store=MemoryStore(config.workspace);records=seed(store,saved_candidate('事实甲'),saved_candidate('事实乙'))
    ids=[r['memory_id'] for r in records];before=store.snapshot()
    with pytest.raises(ValueError):
        store.consolidate([{'action':'mark_disputed','ids':[new_id('memory')]}],before,ids)
    assert store.snapshot()==before
    store.consolidate([{'action':'mark_disputed','ids':ids}],before,ids)
    assert all(r['status']=='disputed' for r in store.snapshot()['records'])
    store.add_candidates([saved_candidate('事实甲')],new_id('turn'))
    assert all(r['status']=='disputed' for r in store.snapshot()['records'])


def test_busy_lock_is_bounded_and_bad_store_not_overwritten(config):
    store=MemoryStore(config.workspace);seed(store)
    fd=os.open(store.root/'store.lock',os.O_RDWR)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        with pytest.raises(MemoryError,match='正忙'):store.add_candidates([],new_id('turn'))
    finally:os.close(fd)
    path=store.root/'store.json';path.write_text('broken')
    with pytest.raises(MemoryError):store.add_candidates([],new_id('turn'))
    assert path.read_text()=='broken'


def test_atomic_write_failure_preserves_store(config,monkeypatch):
    store=MemoryStore(config.workspace);seed(store);before=store.snapshot()
    def fail(*a,**k):raise OSError('disk full')
    monkeypatch.setattr('mini_claude_code.storage.memory_store.os.replace',fail)
    with pytest.raises(MemoryError):store.add_candidates([saved_candidate('新的事实')],new_id('turn'))
    assert store.snapshot()==before
    assert not list(store.root.glob('.pending-*'))


@pytest.mark.parametrize('error',[ModelError('failure'),KeyboardInterrupt(),Cancelled()])
def test_extraction_failure_or_cancel_preserves_completed_answer(make_runtime,config,error):
    client=MemoryModel(answer('最终答复已完成'),extractor=error)
    runtime=make_runtime(client,config=replace(config,memory_enabled=True))
    assert runtime.run(PREFERENCE)==TurnOutcome.COMPLETED
    assert runtime.state.messages[-1].content[0].text=='最终答复已完成'
    assert not (runtime.memory.store.root/'store.json').exists()
    assert len([x for x in client.aux if x[0]=='extract'])==1


def test_aux_request_retry_limit_two_and_no_fallback(make_runtime,config):
    store=MemoryStore(config.workspace);seed(store)
    client=MemoryModel(answer(),selector=ModelError('retry',retryable=True))
    cfg=replace(config,memory_enabled=True,fallback_model='must-not-use')
    runtime=make_runtime(client,config=cfg,wait=lambda _:None)
    runtime.run('Python 测试约定')
    aux=[request for purpose,request,_ in client.aux if purpose=='select']
    assert len(aux)==2 and all(r.model==cfg.model and not r.tools and r.max_output_tokens==300 for r in aux)


def test_corrupt_memory_does_not_block_main_or_create_replacement(make_runtime,config):
    store=MemoryStore(config.workspace);store.root.mkdir(parents=True)
    path=store.root/'store.json';path.write_text('{"schema_version":999}')
    client=MemoryModel(answer())
    runtime=make_runtime(client,config=replace(config,memory_enabled=True))
    assert runtime.run(PREFERENCE)==TurnOutcome.COMPLETED
    assert path.read_text()=='{"schema_version":999}' and not client.aux


def test_external_symlink_store_is_rejected(config,tmp_path):
    outside=tmp_path/'outside.json';outside.write_text('not a store')
    store=MemoryStore(config.workspace);store.root.mkdir(parents=True)
    (store.root/'store.json').symlink_to(outside)
    with pytest.raises(MemoryError):store.snapshot()
    with pytest.raises(MemoryError):store.add_candidates([],new_id('turn'))
    assert outside.read_text()=='not a store'


def test_user_question_is_not_evidence_of_preference(config):
    quote='这个项目以后偏好哪个测试框架？'
    message=Message(MessageRole.USER,[TextBlock(quote)])
    service=MemoryService(config,MemoryModel())
    with pytest.raises(MemoryError):
        service._candidate(candidate(message.message_id,quote=quote),{message.message_id:message},
                           new_id('session'),new_id('turn'))


@pytest.mark.parametrize('proposal',[
    {'action':'merge','ids':[{}]},
    {'action':[],'ids':[]},
    {'action':'write_file','ids':['unknown'],'path':'evil'},
])
def test_malformed_consolidation_is_rejected_without_writing(config,proposal):
    store=MemoryStore(config.workspace);record=seed(store)[0];before=store.snapshot()
    with pytest.raises(ValueError):store.consolidate([proposal],before,[record['memory_id']])
    assert store.snapshot()==before


def test_capacity_does_not_evict_existing_records(config):
    store=MemoryStore(config.workspace)
    for start in range(0,200,5):
        store.add_candidates([saved_candidate(f'project fact {n}') for n in range(start,start+5)],new_id('turn'))
    before=deepcopy(store.snapshot()['records'])
    result=store.add_candidates([saved_candidate('new fact over capacity')],new_id('turn'))
    assert result['capacity'] and result['added']==0 and store.snapshot()['records']==before


def test_merge_preserves_stable_body_and_supersede_needs_current_user_evidence(config):
    store=MemoryStore(config.workspace)
    records=seed(store,saved_candidate('事实甲'),saved_candidate('同义事实甲'))
    ids=[r['memory_id'] for r in records];snapshot=store.snapshot()
    store.consolidate([{'action':'merge','ids':ids,'keep_id':ids[0]}],snapshot,ids)
    merged=store.snapshot()['records']
    assert merged[0]['body']=='事实甲' and len(merged[0]['sources'])==2
    assert merged[1]['status']=='superseded' and merged[1]['superseded_by']==ids[0]
    fresh=saved_candidate('以后改用 unittest')
    fresh['sources'][0]['evidence']='以后测试改为 unittest，不再使用 pytest。'
    seed(store,fresh)
    snapshot=store.snapshot();new=snapshot['records'][-1]
    store.consolidate([{'action':'supersede','ids':[ids[0],new['memory_id']],'keep_id':new['memory_id']}],
                      snapshot,[ids[0],new['memory_id']])
    assert store.snapshot()['records'][0]['status']=='superseded'


def test_auto_consolidation_first_ten_then_five_changes(make_runtime,config):
    store=MemoryStore(config.workspace)
    for n in range(9):seed(store,saved_candidate(f'Python 测试格式 {n}'))
    def extract(data):
        msg=next(m for m in data['source_records'] if m['role']=='user')
        text=msg['content'][0]['text']
        return [candidate(msg['message_id'],quote=text,body=text)]
    client=MemoryModel(*[answer() for _ in range(6)],selector=[],extractor=extract,organizer=[])
    rt=make_runtime(client,config=replace(config,memory_enabled=True))
    for n in range(5):rt.run(f'请记住：以后 Python 测试格式新增项目约定 {n+10}。')
    assert len([x for x in client.aux if x[0]=='consolidate'])==1
    rt.run('请记住：以后 Python 测试格式新增项目约定 15。')
    calls=[request for purpose,request,_ in client.aux if purpose=='consolidate']
    assert len(calls)==2 and all(r.max_output_tokens==3000 and not r.tools for r in calls)


def test_recall_metadata_only_max_five_whole_bodies_and_character_budget(make_runtime,config):
    store=MemoryStore(config.workspace)
    items=[saved_candidate(f'BODY_ONLY_MARKER_{i} '+'x'*1950) for i in range(6)]
    for item in items:seed(store,item)
    def select(data):
        assert 'BODY_ONLY_MARKER' not in json.dumps(data)
        return [r['memory_id'] for r in data['directory'][:5]]
    client=MemoryModel(answer(),selector=select)
    rt=make_runtime(client,config=replace(config,memory_enabled=True,context_budget_chars=12000))
    rt.run('Python 测试约定')
    assert sum(len(r['body']) for r in rt.memory.selected)<=6000
    assert all(r['body'] in client.requests[0].system for r in rt.memory.selected)
    from mini_claude_code.context.budget import chars
    req=client.requests[0]
    assert chars(req.system,req.tools,req.messages)<=12000


@pytest.mark.parametrize('outcome',['failed','cancelled','limit_reached'])
def test_noncompleted_turns_do_not_extract(make_runtime,config,outcome):
    if outcome=='failed':main=ModelError('failure')
    elif outcome=='cancelled':main=KeyboardInterrupt()
    else:main=ModelResponse([ToolCall('glob','glob',{'pattern':'*'})],StopReason.TOOL_CALL)
    client=MemoryModel(main)
    rt=make_runtime(client,config=replace(config,memory_enabled=True),max_rounds=1)
    if outcome=='failed':
        with pytest.raises(ModelError):rt.run(PREFERENCE)
    else:rt.run(PREFERENCE)
    assert not client.aux and not (rt.memory.store.root/'store.json').exists()
