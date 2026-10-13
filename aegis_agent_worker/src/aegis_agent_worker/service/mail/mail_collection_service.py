"""ReAct只读工具执行及审计，不调用模型。"""
from time import monotonic
from uuid import uuid4
from aegis_agent_worker.config.database import TenantContext,tenant_transaction
from aegis_agent_worker.repository.tool_repository import ToolCallRepository
from aegis_agent_worker.tool.contracts import ToolInvocation,ToolError


def safe_evidence(value,depth=0):
    """工具输入作为数据，白名单及大小约束后才可进入 Checkpoint。"""
    fields={"source","provider_message_id","sender_email","subject","received_at","source_version",
            "messages","events","available_slots","start_at","end_at","start_at_local","end_at_local",
            "query_range","duration_minutes","title","participant_resolution"}
    if depth>4:
        return None
    if isinstance(value,dict):
        return {key:(item if key in {"provider_message_id","source_version"} and isinstance(item,str)
                         and len(item)<=512 else safe_evidence(item,depth+1))
                for key,item in value.items() if key in fields}
    if isinstance(value,list):
        return [safe_evidence(item,depth+1) for item in value[:10]]
    if isinstance(value,str):
        return value[:200]
    return value if isinstance(value,(int,float,bool)) or value is None else None


class MailCollectionService:
    def __init__(self,database,runs,tools,events=None):
        self._database,self._runs,self._tools,self._events=database,runs,tools,events
        self._calls=ToolCallRepository()

    async def invoke(self,run,decision,mail_connection_id):
        context=TenantContext(tenant_id=run.tenant_id,user_id=run.user_id)
        started=monotonic()
        async with self._database.session() as session:
            async with tenant_transaction(session,context):
                prepared=await self._tools.prepare(session,tenant_id=run.tenant_id,user_id=run.user_id,
                    run_id=run.id,invocation=ToolInvocation(decision.tool_name,decision.arguments),
                    required_connection_id=mail_connection_id if decision.tool_name.startswith("mail.") else None)
                definition=prepared.registered.definition
                step=await self._runs.create_running_tool_step(session,run_id=run.id,tenant_id=run.tenant_id,
                    # 每次读取（包括用户回答后的事实核验）都是独立审计步骤，不能复用固定键。
                    step_key=f"reply_collect_{uuid4().hex}",label="收集起草所需信息")
                call=await self._calls.create_running(session,tenant_id=run.tenant_id,run_id=run.id,
                    step_id=step,connection_id=prepared.context.connection_id,tool_name=definition.name,
                    risk_level=definition.risk_level,tool_version=definition.version,mcp_server=definition.mcp_server,
                    request_payload=decision.arguments,input_summary={"tool_name":definition.name})
        try:
            result=await self._tools.invoke(prepared)
        except Exception as error:
            async with self._database.session() as session:
                async with tenant_transaction(session,context):
                    await self._calls.fail(session,tool_call_id=call,tenant_id=run.tenant_id,
                        error_code=getattr(error,"code","TOOL_FAILED"),error_message="信息收集工具调用失败",
                        duration_ms=round((monotonic()-started)*1000))
                    await self._runs.finish_step(session,step_id=step,run_id=run.id,tenant_id=run.tenant_id,
                                                status="failed",detail="信息收集失败")
            raise
        summary=safe_evidence(result.response_payload)
        async with self._database.session() as session:
            async with tenant_transaction(session,context):
                await self._calls.succeed(session,tool_call_id=call,tenant_id=run.tenant_id,
                    response_payload=summary,output_summary=summary,duration_ms=round((monotonic()-started)*1000))
                await self._runs.finish_step(session,step_id=step,run_id=run.id,tenant_id=run.tenant_id,
                                            status="succeeded",detail="已取得补充信息")
                event=await self._runs.append_event(session,run_id=run.id,tenant_id=run.tenant_id,
                    event_type="tool_preview",payload={"tool_call_id":str(call),"tool_name":definition.name,
                    "status":"succeeded","risk_level":"read","output_summary":summary})
                if self._events:
                    self._events.publish_after_commit(event)
        return result.response_payload
