"""Bounded real DeepSeek A/B/C demonstrations; host checks fixed fixtures only.

Run from an installed product: python /absolute/path/examples/demo_real.py --real --scenario A
No automatic Shell approval. All workspaces are newly created temporary directories.
"""
import argparse
from dataclasses import replace
from pathlib import Path
import subprocess
import sys
import tempfile

from mini_claude_code.config import load_config,configure_profile
from mini_claude_code.runtime import Runtime
from mini_claude_code.core.loop import TurnOutcome,validate_history
from mini_claude_code.core.messages import ToolResult,TextBlock


def display(kind,value):
    if kind=='call':print('tool:',value.name,flush=True)
    if kind=='notice':print(str(value)[:250],flush=True)


def scenario_a(root):
    (root/'arithmetic.py').write_text('def add(a, b):\n    return a - b\n')
    tests='import unittest\nfrom arithmetic import add\nclass Tests(unittest.TestCase):\n def test_add(self): self.assertEqual(add(2,3),5)\n'
    (root/'test_arithmetic.py').write_text(tests)
    before=subprocess.run([sys.executable,'-m','unittest','-v'],cwd=root,capture_output=True,text=True,timeout=30)
    assert before.returncode!=0
    cfg=configure_profile(load_config(workspace=root),minimal=True)
    with Runtime(cfg,interactive=False,stream=False,display=display,max_rounds=8) as r:
        outcome=r.run('Read arithmetic.py and fix add(a,b) to return a+b. Edit only arithmetic.py, then read it back. Do not run Shell. Do not modify tests. The host will run fixed unittest checks after your final answer. Keep answer short and honest about not running tests yourself.')
        assert outcome==TurnOutcome.COMPLETED
        validate_history(r.state.messages)
        assert any(b.status.value=='success' and b.metadata.get('tool_name') in {'edit_file','write_file'} for m in r.state.messages for b in m.content if isinstance(b,ToolResult))
        sid=r.state.session_id
    assert (root/'test_arithmetic.py').read_text()==tests
    after=subprocess.run([sys.executable,'-m','unittest','-v'],cwd=root,capture_output=True,text=True,timeout=30)
    assert after.returncode==0,after.stderr
    with Runtime(cfg,interactive=False,stream=False,resume=sid,max_rounds=1) as resumed:
        validate_history(resumed.state.messages)
        assert resumed.events is None
    print('PASS A: real model repaired source; fixed host unittest failed before and passed after; tests unchanged; resume valid.',flush=True)


def scenario_b(root):
    (root/'project.txt').write_text('Project requirement: normalize whitespace with str.strip().\n')
    cfg=replace(load_config(workspace=root),memory_enabled=False)
    with Runtime(cfg,interactive=False,stream=False,display=display,max_rounds=6) as r:
        outcome=r.run('Use task exactly once in analyze mode: ask the isolated child to read project.txt and report the requirement without edits or Shell. After receiving its report, answer in one sentence. Do not use Shell or persistent teammates.')
        assert outcome==TurnOutcome.COMPLETED
        outputs=[b for m in r.state.messages for b in m.content if isinstance(b,ToolResult) and b.metadata.get('tool_name')=='task']
        assert outputs and outputs[0].status.value=='success' and 'strip' in outputs[0].output.lower()
        assert r.task_runner.count==1 and not r.task_runner.active
        validate_history(r.state.messages)
    print('PASS B: real parent delegated once to isolated analyze child; report returned through common executor.',flush=True)


def scenario_c(root):
    cfg=load_config(workspace=root)
    with Runtime(cfg,interactive=False,stream=False,display=display,max_rounds=2) as r:
        assert r.run('请记住：这个项目长期偏好使用 pytest 编写 Python 单元测试。请简短确认，不调用工具。')==TurnOutcome.COMPLETED
        assert any('pytest' in row['body'] for row in r.memory.store.snapshot()['records'])
    with Runtime(cfg,interactive=False,stream=False,display=display,max_rounds=2) as r:
        assert r.run('这个项目编写 Python 单元测试时采用哪一个框架？只回答，不调用工具。')==TurnOutcome.COMPLETED
        assert r.memory.selected and any('pytest' in row['body'] for row in r.memory.selected)
        assert 'pytest' in ''.join(b.text for b in r.state.messages[-1].content if isinstance(b,TextBlock)).lower()
    print('PASS C: real extraction and new-session recall confirmed against memory store and answer.',flush=True)


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--real',action='store_true',required=True);p.add_argument('--scenario',choices=['A','B','C'],required=True)
    args=p.parse_args()
    with tempfile.TemporaryDirectory(prefix='mini-cc-m4-real-') as temp:
        print('Temporary workspace:',temp,flush=True)
        {'A':scenario_a,'B':scenario_b,'C':scenario_c}[args.scenario](Path(temp))

if __name__=='__main__':main()
