"""Real DeepSeek two-worktree demonstration. Explicit --real; no model Shell approval."""
import argparse
from pathlib import Path
from dataclasses import replace
import tempfile,subprocess,time,json,threading
from mini_claude_code.config import load_config
from mini_claude_code.runtime import Runtime
from mini_claude_code.core.loop import validate_history
from mini_claude_code.core.messages import ToolCall,ToolStatus
from mini_claude_code.core._validation import new_id


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--real",action="store_true",required=True)
    parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='mini-cc-m4-team-') as tmp:
        root=Path(tmp)
        def git(*args):return subprocess.run(['git',*args],cwd=root,check=True,capture_output=True,text=True).stdout.strip()
        git('init','-q');git('config','user.name','M3 demo');git('config','user.email','m3@example.invalid')
        (root/'arithmetic.py').write_text('def add(a, b):\n    return a - b\n')
        (root/'text_utils.py').write_text('def normalize(value):\n    return value\n')
        (root/'test_functions.py').write_text('import unittest\nfrom arithmetic import add\nfrom text_utils import normalize\nclass Tests(unittest.TestCase):\n def test_add(self): self.assertEqual(add(2,3),5)\n def test_normalize(self): self.assertEqual(normalize(" x "),"x")\n')
        git('add','.');git('commit','-qm','fixture')
        cfg=replace(load_config(workspace=root),memory_enabled=False)
        cfg.require_model_ready()
        def display(kind,value):
            if kind=='call':print('lead tool:',value.name,flush=True)
            elif kind=='notice':print(str(value)[:150],flush=True)
        with Runtime(cfg,interactive=True,approve=lambda *a:False,display=display,teams=True,stream=False,max_rounds=12) as lead:
            def execute(name,args):
                result=lead.executor.execute(ToolCall(new_id('message'),name,args),lead.context,assistant_message_id=new_id('message'))
                if result.status!=ToolStatus.SUCCESS:raise RuntimeError(result.output)
                return result
            descriptions=[('fix-add','Fix arithmetic.py add(a,b): return a+b. Read file, edit only arithmetic.py. Do not run Shell or delegate. Use complete_task only after reading back your change, with an honest report that tests are not run.'),
                          ('fix-normalize','Fix text_utils.py normalize(value): return value.strip(). Read file, edit only text_utils.py. Do not run Shell or delegate. Use complete_task only after reading back your change, with an honest report that tests are not run.')]
            tasks=[]
            for i,(subject,desc) in enumerate(descriptions):
                task=lead.tasks.store.create(subject,desc,lead.tasks.source('host'),team_id=lead.team_id);tasks.append(task)
                execute('create_worktree',{'name':'demo'+str(i),'task_id':task.task_id})
            # The real lead must create teammates using its ordinary common loop.
            prompt='The user explicitly enabled teams. Dispatch these already bound independent tasks now using spawn_teammate, then finish your answer. Do not poll or run Shell. Do not claim tasks yourself. No merge or commit. '
            prompt+=' '.join('Spawn editor'+str(i)+' for task '+t.task_id+' with require_plan=false, auto_claim=false.' for i,t in enumerate(tasks))
            lead.run(prompt)
            deadline=time.monotonic()+240
            while time.monotonic()<deadline:
                graph=lead.tasks.store.snapshot()
                if all(graph[t.task_id].status=='completed' for t in tasks):break
                if any(w.stopped.is_set() and w.error for w in lead.team.workers.values()):raise RuntimeError('Teammate failed')
                if lead.events.pending():lead.events.dispatch()
                else:
                    import selectors
                    with selectors.DefaultSelector() as selector:
                        selector.register(lead.events.wakeup.read_fd,selectors.EVENT_READ);selector.select(1)
                    lead.events.wakeup.drain()
            else:raise RuntimeError('real demo timed out')
            # Drain final delivery after actual task completion; no model polling for status.
            for _ in range(10):
                if all(lead.team.member(w.agent_id)['state']=='IDLE' for w in lead.team.workers.values()):break
                threading.Event().wait(.05)
            if lead.events.pending():lead.events.dispatch()
            roots=[root/'.mini-claude-code/checkouts'/('demo'+str(i)) for i in range(2)]
            assert 'return a + b' in (roots[0]/'arithmetic.py').read_text()
            assert 'strip()' in (roots[1]/'text_utils.py').read_text()
            assert (root/'arithmetic.py').read_text()=='def add(a, b):\n    return a - b\n'
            assert (root/'text_utils.py').read_text()=='def normalize(value):\n    return value\n'
            # Fixed host checks run only the relevant supplied unittest case in each independent checkout.
            for i,module_case in enumerate(('test_functions.Tests.test_add','test_functions.Tests.test_normalize')):
                import sys
                proc=subprocess.run([sys.executable,'-m','unittest',module_case],cwd=roots[i],capture_output=True,text=True,timeout=30)
                assert proc.returncode==0,proc.stderr
                print('fixed host verification passed:',module_case,flush=True)
            for w in lead.team.workers.values():validate_history(w.runtime.state.messages)
            validate_history(lead.state.messages)
            assert len(lead.team.workers)==2
            assert any(r['status']=='accepted' and r['event']['kind']=='team_status' for r in lead.events.store.list(lead.state.session_id))
            print('PASS: real DeepSeek lead dispatched two persistent teammates; isolated checkout edits, completion events accepted; fixed host unittest cases passed. No model Shell approvals or merges.',flush=True)
        assert all(w.stopped.is_set() for w in lead.team.workers.values())
        print('PASS: all teammate threads stopped on CLI-lifetime close.',flush=True)

if __name__=="__main__":main()
