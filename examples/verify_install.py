"""Run with a fresh wheel environment's Python from a directory outside the repo.

Offline SDK MockTransport drives the installed CLI. It never contacts a model.
"""
import builtins
from contextlib import redirect_stdout,redirect_stderr
import io
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading

import anthropic
import httpx
import mini_claude_code
from mini_claude_code import cli,runtime
from mini_claude_code.config import DEFAULT_ENV_FILE,load_config
from mini_claude_code.providers.deepseek import DeepSeekClient


def main():
    assert Path(mini_claude_code.__file__).is_relative_to(Path(sys.prefix)),mini_claude_code.__file__
    assert DEFAULT_ENV_FILE==Path.home()/'.config/mini-claude-code/.env'
    socket.socket.connect=lambda *a,**k:(_ for _ in ()).throw(AssertionError('unexpected network'))
    before=threading.enumerate()
    os.environ['DEEPSEEK_API_KEY']='offline-install-key'
    with tempfile.TemporaryDirectory(prefix='mini-cc-wheel-check-') as temp:
        root=Path(temp);(root/'.env').write_text('MODEL_ID=wrong-project-env\n')
        assert load_config(workspace=root,environ={}).model!='wrong-project-env'
        output=io.StringIO()
        with redirect_stdout(output),redirect_stderr(output):
            for argv in (['--help'],['--version']):
                try:cli.main(argv)
                except SystemExit as exc:assert exc.code==0
            for args in (['check'],['sessions','list'],['tasks','list'],['memory','list'],['schedule','list'],['worktrees','list']):
                assert cli.main(['--workspace',str(root),*args])==0
        assert not (root/'.mini-claude-code').exists() and threading.enumerate()==before
        count=[]
        def transport(request):
            data=json.loads(request.content);count.append(data)
            assert request.url.path.endswith('/v1/messages')
            content=[{'type':'tool_use','id':'install-write','name':'write_file','input':{'path':'installed.txt','content':'wheel works'}}] if len(count)==1 else [{'type':'text','text':'installed CLI verified'}]
            return httpx.Response(200,json={'id':'msg_install','type':'message','role':'assistant','model':'deepseek-flash','content':content,'stop_reason':'tool_use' if len(count)==1 else 'end_turn','stop_sequence':None,'usage':{'input_tokens':1,'output_tokens':1}})
        def factory(cfg):
            c=DeepSeekClient(cfg);c._client.close()
            c._client=anthropic.Anthropic(api_key='offline-install-key',base_url=cfg.base_url,max_retries=0,http_client=httpx.Client(transport=httpx.MockTransport(transport)))
            return c
        runtime.DeepSeekClient=factory
        old_stdin,old_input=sys.stdin,builtins.input;sys.stdin=io.StringIO()
        lines=iter(['write the fixed fixture','/exit']);builtins.input=lambda prompt='':next(lines)
        try:
            with redirect_stdout(output),redirect_stderr(output):
                assert cli.main(['--workspace',str(root),'chat','--minimal','--no-stream'])==0
        finally:sys.stdin=old_stdin;builtins.input=old_input
        assert (root/'installed.txt').read_text()=='wheel works' and len(count)==2
        assert threading.enumerate()==before
    print('PASS: independent wheel import, user config path, target .env ignored, help/version/check/management side-effect checks, installed CLI SDK MockTransport write and exit.')

if __name__=='__main__':main()
