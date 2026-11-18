import json
from dataclasses import replace

import anthropic
import httpx
import pytest

from conftest import ScriptedModelClient,answer
from mini_claude_code.core.errors import ContractError,ModelError
from mini_claude_code.core.messages import Message,TextBlock,ToolCall,ToolResult,ToolSpec
from mini_claude_code.core.model import ModelRequest,StopReason,ModelResponse
from mini_claude_code.providers.deepseek import DeepSeekClient,parse_response,request_messages


def sse(event,data):
    return f'event: {event}\ndata: {json.dumps({"type":event,**data})}\n\n'


def frames(stop='tool_use',finish=True,fragment='{"path":"x"}'):
    text=sse('message_start',{'message':{'id':'msg_demo','type':'message','role':'assistant','model':'deepseek-flash','content':[],
            'stop_reason':None,'stop_sequence':None,'usage':{'input_tokens':12,'output_tokens':0}}})
    text+=sse('content_block_start',{'index':0,'content_block':{'type':'text','text':''}})
    text+=sse('content_block_delta',{'index':0,'delta':{'type':'text_delta','text':'inspect'}})
    text+=sse('content_block_stop',{'index':0})
    text+=sse('content_block_start',{'index':1,'content_block':{'type':'tool_use','id':'read1','name':'read_file','input':{}}})
    text+=sse('content_block_delta',{'index':1,'delta':{'type':'input_json_delta','partial_json':fragment}})
    text+=sse('content_block_stop',{'index':1})
    if finish:
        text+=sse('message_delta',{'delta':{'stop_reason':stop,'stop_sequence':None},'usage':{'output_tokens':22}})
        text+=sse('message_stop',{})
    return text


def sdk(config,transport):
    client=DeepSeekClient(config);client._client.close()
    client._client=anthropic.Anthropic(api_key='offline-test-key',base_url=config.base_url,max_retries=0,
                                      http_client=httpx.Client(transport=transport))
    return client


def request():
    return ModelRequest('system',[Message('user',[TextBlock('inspect')])],
                        [ToolSpec('read_file','Read text',{'type':'object'})],'deepseek-flash',8000)


def test_real_sdk_sse_collects_before_dispatch(config):
    requests=[]
    def handle(req):
        body=json.loads(req.content);requests.append(body)
        assert req.url.path=='/anthropic/v1/messages'
        assert body['thinking']=={'type':'disabled'} and body['stream'] is True
        assert body['system']=='system' and body['tools'][0]['input_schema']=={'type':'object'}
        return httpx.Response(200,headers={'content-type':'text/event-stream'},text=frames())
    client=sdk(config,httpx.MockTransport(handle))
    try:
        items=list(client.stream(request()))
        assert [e.kind for e in items]==['text','final']
        assert items[0].text=='inspect'
        response=items[-1].response
        assert response.content[1].arguments=={'path':'x'}
        assert response.usage.input_tokens==12 and response.usage.output_tokens==22
        assert client._active_stream is None and len(requests)==1
    finally:client.close()


@pytest.mark.parametrize('finish,fragment,stop,error',[(False,'{}','tool_use',ModelError),
    (True,'{','tool_use',ContractError),(True,'{"path":"x","path":"y"}','tool_use',ContractError),
    (True,'{}','refusal',ContractError)])
def test_invalid_streams_no_final_response(config,finish,fragment,stop,error):
    client=sdk(config,httpx.MockTransport(lambda req:httpx.Response(200,headers={'content-type':'text/event-stream'},text=frames(stop,finish,fragment))))
    try:
        with pytest.raises(error):list(client.stream(request()))
        assert client._active_stream is None
    finally:client.close()


def test_truncated_stream_does_not_parse_partial_tool_json(config):
    client=sdk(config,httpx.MockTransport(lambda req:httpx.Response(200,headers={'content-type':'text/event-stream'},text=frames('max_tokens',True,'{'))))
    try:
        response=list(client.stream(request()))[-1].response
        assert response.stop_reason==StopReason.OUTPUT_TRUNCATED and not response.content
    finally:client.close()


def test_http_error_metadata_and_single_sdk_attempt(config):
    requests=[]
    def handle(req):
        requests.append(req)
        return httpx.Response(429,headers={'request-id':'req_example','retry-after':'99'},
                              json={'type':'error','error':{'type':'rate_limit_error','message':'hidden-secret'}})
    client=sdk(config,httpx.MockTransport(handle))
    try:
        with pytest.raises(ModelError) as caught:client.generate(request())
        error=caught.value
        assert error.status_code==429 and error.provider_request_id=='req_example' and error.retry_after==30
        assert 'hidden-secret' not in str(error) and len(requests)==1
    finally:client.close()


def test_internal_wire_roundtrip_and_error_status():
    call=ToolCall('opaque/id','read_file',{'path':'x'})
    history=[Message('user',[TextBlock('read')]),Message('assistant',[TextBlock('before'),call,TextBlock('after')]),
             Message('tool',[ToolResult(call.call_id,'denied','denied',{'exit_code':7})])]
    req=replace(request(),messages=history)
    restored=ModelRequest.from_dict(json.loads(json.dumps(req.to_dict())))
    assert restored==req
    wire=request_messages(req)
    assert [b['type'] for b in wire[1]['content']]==['text','tool_use','text']
    assert wire[2]['role']=='user' and wire[2]['content'][0]['tool_use_id']==call.call_id
    assert wire[2]['content'][0]['is_error'] and '"exit_code": 7' in wire[2]['content'][0]['content']
    with pytest.raises(ContractError):Message.from_dict(history[0].to_dict()|{'schema_version':99})


def test_duplicate_call_ids_rejected_before_any_side_effect(make_runtime,tmp_path):
    # Mutating a nested list simulates an invalid adapter response at the loop boundary.
    response=ModelResponse([ToolCall('same','write_file',{'path':'no','content':'bad'})],StopReason.TOOL_CALL)
    response.content.append(ToolCall('same','write_file',{'path':'no2','content':'bad'}))
    runtime=make_runtime(ScriptedModelClient(response))
    with pytest.raises(ContractError):runtime.run('write')
    assert len(runtime.state.messages)==1 and not (tmp_path/'no').exists()


def test_stream_typed_overload_on_http_200_is_retryable(config):
    data='event: error\ndata: '+json.dumps({'type':'error','error':{'type':'overloaded_error','message':'do-not-display'}})+'\n\n'
    client=sdk(config,httpx.MockTransport(lambda req:httpx.Response(200,headers={'content-type':'text/event-stream'},text=data)))
    try:
        with pytest.raises(ModelError) as caught:list(client.stream(request()))
        assert caught.value.retryable and caught.value.error_type=='overloaded_error'
        assert 'do-not-display' not in str(caught.value)
    finally:client.close()


def test_malformed_sse_is_protocol_error_and_stream_is_closed(config):
    client=sdk(config,httpx.MockTransport(lambda req:httpx.Response(200,headers={'content-type':'text/event-stream'},text='event: message_start\ndata: {bad-json\n\n')))
    try:
        with pytest.raises(ContractError):list(client.stream(request()))
        assert client._active_stream is None
    finally:client.close()
