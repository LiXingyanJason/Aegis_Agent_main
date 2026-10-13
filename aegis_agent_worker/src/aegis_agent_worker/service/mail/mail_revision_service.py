"""草稿修改事务与事件，模型调用留在工作流。"""
from aegis_agent_worker.config.database import TenantContext,tenant_transaction
from aegis_agent_worker.repository.mail_revision_repository import MailRevisionRepository


class MailRevisionService:
    def __init__(self,database,runs,events=None):
        self._database,self._runs,self._events=database,runs,events
        self._repository=MailRevisionRepository()
    async def load(self,run):
        async with self._database.session() as session:
            async with tenant_transaction(session,TenantContext(tenant_id=run.tenant_id,user_id=run.user_id)):
                return await self._repository.load(session,run)
    async def save(self,run,draft,body):
        async with self._database.session() as session:
            async with tenant_transaction(session,TenantContext(tenant_id=run.tenant_id,user_id=run.user_id)):
                row=await self._repository.save(session,run,draft,body)
                for kind,payload in (
                    ("mail_draft_revision_completed",{"draft_id":str(row["id"]),"version":row["version"]}),
                    ("run_completed",{"status":"completed","result_summary":"草稿修改完成"}),
                ):
                    event=await self._runs.append_event(session,run_id=run.id,tenant_id=run.tenant_id,
                                                       event_type=kind,payload=payload)
                    if self._events:
                        self._events.publish_after_commit(event)
                return row
