"""Minimal real stdio server for the phase 9 demo and future M4 fixtures.

Run with the project's Python environment. stdout is protocol-only. The write
utility is deliberately restricted to a temporary cwd (or explicit env root).
This is a test fixture, not a generic filesystem server or a deployment tool.
"""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile

from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server

server=Server('mini-cc-example')


def tool(name,description,schema,output=None):
    return types.Tool(name=name,description=description,inputSchema=schema,outputSchema=output)


@server.list_tools()
async def list_tools():
    return [
        tool('query','Return a bounded echo plus actual process cwd; pure query.',{
            'type':'object','properties':{'text':{'type':'string','maxLength':1000}},'required':['text'],'additionalProperties':False},
            {'type':'object','properties':{'echo':{'type':'string'},'cwd':{'type':'string'}},'required':['echo','cwd'],'additionalProperties':False}),
        tool('write_note','Write only inside a temporary allowed root; no overwrite.',{
            'type':'object','properties':{'path':{'type':'string','minLength':1},'content':{'type':'string','maxLength':10000}},
            'required':['path','content'],'additionalProperties':False}),
        tool('controlled_failure','Return isError=true or a finite delay for host cancellation checks.',{
            'type':'object','properties':{'delay':{'type':'number','minimum':0,'maximum':120,'default':0}},'additionalProperties':False}),
    ]


@server.call_tool(validate_input=True)
async def call_tool(name,arguments):
    if name=='query':
        value={'echo':arguments['text'],'cwd':str(Path.cwd())}
        return types.CallToolResult(content=[types.TextContent(type='text',text='query completed')],structuredContent=value,isError=False)
    if name=='write_note':
        root=Path(os.environ.get('MCP_EXAMPLE_ROOT',str(Path.cwd()))).resolve()
        if not root.is_relative_to(Path(tempfile.gettempdir()).resolve()):
            return types.CallToolResult(content=[types.TextContent(type='text',text='Fixture write root must be in a temporary directory.')],isError=True)
        value=Path(arguments['path']);target=(value if value.is_absolute() else root/value).resolve()
        if not target.is_relative_to(root) or '.git' in target.relative_to(root).parts or '.mini-claude-code' in target.relative_to(root).parts:
            return types.CallToolResult(content=[types.TextContent(type='text',text='Denied fixture path')],isError=True)
        target.parent.mkdir(parents=True,exist_ok=True)
        try:
            with target.open('x',encoding='utf-8') as stream:stream.write(arguments['content'])
        except FileExistsError:
            return types.CallToolResult(content=[types.TextContent(type='text',text='Existing file preserved')],isError=True)
        return [types.TextContent(type='text',text='Wrote temporary note: '+str(target.relative_to(root)))]
    if name=='controlled_failure':
        await asyncio.sleep(arguments.get('delay',0))
        return types.CallToolResult(content=[types.TextContent(type='text',text='Controlled fixture failure; not retried.')],isError=True)
    raise ValueError('Unknown fixture tool')


async def main():
    print('mini-cc example MCP server started; stderr is a log, not a protocol failure',file=sys.stderr,flush=True)
    async with stdio_server() as (read_stream,write_stream):
        await server.run(read_stream,write_stream,server.create_initialization_options())


if __name__=='__main__':asyncio.run(main())
