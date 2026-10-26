"""Actual in-process MCP server/Mock Provider; response loss injected at adapter.

Not a TCP proxy outage or proof of provider reconciliation in production.
"""
import json
from dataclasses import replace
from pathlib import Path
import sys
import pytest
from starlette.testclient import TestClient
from mcp.server.transport_security import TransportSecuritySettings
from aegis_calendar_mcp.config import Settings
from aegis_calendar_mcp.server import create_server
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'agent'))
from test_harness_react_revision import Database,RUN
from aegis_agent_worker.harness.executor import HarnessExecutor
from aegis_agent_worker.tool.contracts import ToolDefinition,ToolInvocation,ToolError,ToolResult
from aegis_agent_worker.tool.registry import ToolRegistry
from aegis_agent_worker.tool.gateway import ToolGateway


@pytest.mark.asyncio
@pytest.mark.parametrize('approved,lose_response',[(False,False),(False,True),(True,False),(True,True)])
async def test_actual_mcp_write_and_receipt_loss(tmp_path,approved,lose_response):
    path=tmp_path/'calendar.json';path.write_text('[]')
    settings=Settings(provider='mock',transport='streamable-http',host='127.0.0.1',port=19001,
        streamable_http_path='/mcp',allowed_hosts=('127.0.0.1',),mock_data_path=path)
    app=create_server(settings).streamable_http_app(streamable_http_path='/mcp',json_response=True,stateless_http=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))
    run=replace(RUN,run_type='conversation');calls=[]
    args=dict(title='E08虚构会议',start_at='2026-11-02T10:00:00+08:00',end_at='2026-11-02T10:30:00+08:00',
              attendees=['test@example.com'],idempotency_key='e08-fixed-key')
    with TestClient(app) as http:
        class Handler:
            async def invoke(self,context,arguments):
                name=arguments.pop('__tool_name');calls.append(name)
                response=http.post('/mcp',headers={'Accept':'application/json, text/event-stream'},json={
                    'jsonrpc':'2.0','id':len(calls),'method':'tools/call','params':{'name':name,'arguments':arguments,
                    '_meta':{'aegis_connection_id':str(run.id),'aegis_tenant_id':str(run.tenant_id),
                        'aegis_user_id':str(run.user_id),'aegis_run_id':str(run.id)}}})
                result=response.json()['result'];assert not result['isError']
                if lose_response and len(calls)==1:raise ToolError('TOOL_UNAVAILABLE','injected lost receipt')
                return ToolResult(result['structuredContent'],{})
        registry=ToolRegistry();registry.register(ToolDefinition('calendar.create_event','1','write','mock','test'),Handler())
        db=Database();harness=HarnessExecutor(db);gateway=ToolGateway(registry,harness=harness)
        async with harness.scope(run):
            if not approved:
                with pytest.raises(ToolError) as error:
                    await gateway.prepare(None,tenant_id=run.tenant_id,user_id=run.user_id,run_id=run.id,
                        invocation=ToolInvocation('calendar.create_event',args))
                assert error.value.code=='TOOL_POLICY_BLOCKED' and not calls
                assert json.loads(path.read_text())==[]
                return
            prepared=await gateway.prepare(None,tenant_id=run.tenant_id,user_id=run.user_id,run_id=run.id,
                invocation=ToolInvocation('calendar.create_event',args),allow_confirmed_write=True)
            if lose_response:
                with pytest.raises(ToolError):await gateway.invoke(prepared)
                assert len(calls)==1  # No automatic write retry after lost response.
                assert len(json.loads(path.read_text()))==1
            else:
                await gateway.invoke(prepared)
            # Explicit test replay with same idempotency key, not an automatic app retry.
            receipt=await gateway.invoke(prepared)
            records=json.loads(path.read_text())
            assert len(calls)==2 and len(records)==1
            assert receipt.response_payload['external_event_id']==records[0]['external_event_id']
            assert db.budgets[run.id]['tool_calls']==2
