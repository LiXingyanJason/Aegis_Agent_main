"""固定跨领域链路的来源复核与安全步骤审计；不调用LLM。"""
from time import monotonic
from uuid import uuid4
from aegis_agent_worker.config.database import TenantContext, tenant_transaction
from aegis_agent_worker.repository.cooperation_repository import CooperationRepository
from aegis_agent_worker.repository.tool_repository import ToolCallRepository
from aegis_agent_worker.tool.contracts import ToolInvocation, ToolError
from aegis_agent_worker.agent.contracts.cooperation import TaskStepResult, snapshot_digest


class FixedMeetingService:
    def __init__(self, database, runs, tools, events=None, repository=None):
        self._database,self._runs,self._tools,self._events=database,runs,tools,events
        self._repository=repository or CooperationRepository()
        self._calls=ToolCallRepository()

    async def _source(self, run, reference):
        async with self._database.session() as session:
            async with tenant_transaction(session,TenantContext(tenant_id=run.tenant_id,user_id=run.user_id)):
                source=await self._repository.email_source(session,run,reference)
                if source is None: raise ToolError('COOP_SOURCE_CHANGED','邮件来源已变化或无权访问，请重新选择。')
                return source

    async def read_source(self, run, reference):
        """原邮件仅返回节点局部变量；工具审计只含安全引用，不复制正文。"""
        source=await self._source(run,reference)
        context=TenantContext(tenant_id=run.tenant_id,user_id=run.user_id)
        started=monotonic()
        async with self._database.session() as session:
            async with tenant_transaction(session,context):
                prepared=await self._tools.prepare(session,tenant_id=run.tenant_id,user_id=run.user_id,run_id=run.id,
                    invocation=ToolInvocation('mail.messages.get',{'provider_message_id':source['provider_message_id']}),
                    required_connection_id=source['connection_id'])
                definition=prepared.registered.definition
                step=await self._runs.create_running_tool_step(session,run_id=run.id,tenant_id=run.tenant_id,
                    step_key=f'cooperation_mail_read_{uuid4().hex}',label='核验协同邮件来源')
                call=await self._calls.create_running(session,tenant_id=run.tenant_id,run_id=run.id,step_id=step,
                    connection_id=source['connection_id'],tool_name=definition.name,risk_level=definition.risk_level,
                    tool_version=definition.version,mcp_server=definition.mcp_server,
                    request_payload={'provider_message_id':source['provider_message_id']},input_summary={'email_id':str(reference.id)})
        try:
            response=(await self._tools.invoke(prepared)).response_payload
            if (not isinstance(response,dict) or response.get('provider_message_id')!=source['provider_message_id']
                    or response.get('source_version')!=reference.source_version or not isinstance(response.get('body_text'),str)):
                raise ToolError('COOP_SOURCE_CHANGED','邮件来源版本或正文无效，请重新查询。')
            await self._source(run,reference)
        except Exception as error:
            async with self._database.session() as session:
                async with tenant_transaction(session,context):
                    await self._calls.fail(session,tool_call_id=call,tenant_id=run.tenant_id,error_code=getattr(error,'code','TOOL_FAILED'),
                        error_message='协同来源读取失败',duration_ms=round((monotonic()-started)*1000))
                    await self._runs.finish_step(session,step_id=step,run_id=run.id,tenant_id=run.tenant_id,status='failed',detail='协同来源不可用')
            raise
        safe={'email_id':str(reference.id),'source_version':reference.source_version}
        async with self._database.session() as session:
            async with tenant_transaction(session,context):
                await self._calls.succeed(session,tool_call_id=call,tenant_id=run.tenant_id,
                    response_payload=safe,output_summary=safe,duration_ms=round((monotonic()-started)*1000))
                await self._runs.finish_step(session,step_id=step,run_id=run.id,tenant_id=run.tenant_id,status='succeeded',detail='邮件来源核验完成')
        return response

    async def verify_source(self,run,reference,digest,*,fresh=False):
        source=await self._source(run,reference)
        # 即使不发起网络读取，也重新经过Gateway核验原连接及mail.read；SQL快照不代表连接授权。
        async with self._database.session() as session:
            async with tenant_transaction(session,TenantContext(tenant_id=run.tenant_id,user_id=run.user_id)):
                await self._tools.prepare(session,tenant_id=run.tenant_id,user_id=run.user_id,run_id=run.id,
                    invocation=ToolInvocation('mail.messages.get',{'provider_message_id':source['provider_message_id']}),
                    required_connection_id=source['connection_id'])
        if fresh:
            email=await self.read_source(run,reference)
            if snapshot_digest(email)!=digest:
                raise ToolError('COOP_SOURCE_CHANGED','邮件内容已变化，请重新发起会议安排。')

    async def record(self,run,result):
        result=TaskStepResult.model_validate(result)
        async with self._database.session() as session:
            async with tenant_transaction(session,TenantContext(tenant_id=run.tenant_id,user_id=run.user_id)):
                event=await self._runs.append_event(session,run_id=run.id,tenant_id=run.tenant_id,
                    event_type='cooperation_step_completed',payload=result.model_dump(mode='json'))
                if self._events: self._events.publish_after_commit(event)
