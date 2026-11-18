"""A small-context lead/child workflow followed by cross-session recall."""

from dataclasses import replace
import json

from conftest import answer
from test_memory import MemoryModel,candidate
from test_agents_skills import calls,results
from mini_claude_code.core.messages import ToolCall,ToolResult
from mini_claude_code.core.loop import TurnOutcome,validate_history
from mini_claude_code.context.budget import chars


class HarnessModel(MemoryModel):
    def generate(self,request):
        if request.system.startswith('Summarize the supplied records'):
            return answer('Read-only project work. User prefers pytest; Todo and child analysis ran. Preserve output artifact references.')
        return super().generate(request)


def test_low_budget_todo_skill_subtask_output_compact_then_new_session(make_runtime,config):
    root=config.workspace
    folder=root/'skills/review';folder.mkdir(parents=True)
    (folder/'SKILL.md').write_text('---\nname: review\ndescription: Review Python project facts.\n---\nRead source files; report verified facts only.\n')
    (root/'project.txt').write_text('Project tests use pytest.\n')
    (root/'large.txt').write_text(''.join(f'row {i}: '+'x'*35+'\n' for i in range(1800)))
    child=HarnessModel(calls(ToolCall('child-read','read_file',{'path':'project.txt'})),answer('Verified pytest configuration.'))
    def read_page(request):
        artifact=next(b.metadata['artifact_id'] for m in request.messages for b in m.content
                      if isinstance(b,ToolResult) and b.call_id=='large')
        return calls(ToolCall('page','read_tool_output',{'artifact_id':artifact,'offset':1799,'limit':1}),
                     ToolCall('compact','compact',{'focus':'Keep current user preference, output and actual verification.'}))
    def extract(data):
        user=next(m for m in data['source_records'] if m['role']=='user')
        return [candidate(user['message_id'],quote=user['content'][0]['text'])]
    parent=HarnessModel(
        calls(ToolCall('todo','todo_write',{'todos':[{'content':'Inspect project','status':'in_progress'}]}),
              ToolCall('skill','load_skill',{'name':'review'})),
        calls(ToolCall('task','task',{'description':'Read project.txt and report Python pytest facts'})),
        calls(ToolCall('large','read_file',{'path':'large.txt','limit':2000})),
        read_page,answer('Completed read-only inspection.'),extractor=extract)
    cfg=replace(config,memory_enabled=True,context_budget_chars=19000)
    rt=make_runtime(parent,config=cfg,child_client_factory=lambda _:child)
    assert rt.run('请记住：以后项目统一使用 pytest 编写 Python 测试。')==TurnOutcome.COMPLETED
    validate_history(rt.state.messages)
    assert rt.todos.current()[0].content=='Inspect project'
    assert any('row 1799:' in b.output for b in results(rt) if b.call_id=='page')
    assert rt.context_manager.checkpoint is not None
    assert all(chars(req.system,req.tools,req.messages)<=19000 for req in parent.requests)
    assert len(rt.memory.store.snapshot()['records'])==1 and not child.aux
    next_client=HarnessModel(answer(),selector=lambda data:[data['directory'][0]['memory_id']])
    fresh=make_runtime(next_client,config=cfg)
    fresh.run('查询项目 Python 测试约定')
    assert 'pytest' in fresh.memory.section()
    assert 'Completed read-only inspection' not in json.dumps([m.to_dict() for m in next_client.requests[0].messages])
