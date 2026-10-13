"""补充问题的业务存储与同一 run 恢复，问题不是审批决定。"""
from sqlalchemy import text
from aegis_agent_worker.config.database import TenantContext, tenant_transaction
from aegis_agent_worker.repository.run_repository import ClaimedRun
from aegis_agent_worker.harness.exceptions import HarnessError


class InputRequestService:
    def __init__(self,database,runs,events=None):
        self._database,self._runs,self._events=database,runs,events

    async def prepare(self,run,question):
        async with self._database.session() as session:
            async with tenant_transaction(session,TenantContext(tenant_id=run.tenant_id,user_id=run.user_id)):
                await session.execute(text("SELECT id FROM agent_runs WHERE id=:id FOR UPDATE"),dict(id=run.id))
                result=await session.execute(text("""
                  INSERT INTO run_input_requests(tenant_id,user_id,run_id,question,version)
                  SELECT :tenant,:user,:run,:question,COALESCE(MAX(version),0)+1
                  FROM run_input_requests WHERE run_id=:run RETURNING id,version
                """),dict(tenant=run.tenant_id,user=run.user_id,run=run.id,question=question))
                row=result.mappings().one()
                await session.execute(text("UPDATE agent_runs SET status='waiting_input',current_stage='等待补充信息',updated_at=now() WHERE id=:id"),
                                      dict(id=run.id))
                return {"question_id":str(row["id"]),"version":row["version"],"question":question}

    async def mark_ready(self,run,question_id):
        """ainvoke 返回 interrupt 后再发布问题，此时 Checkpointer 已完成保存。"""
        async with self._database.session() as session:
            async with tenant_transaction(session,TenantContext(tenant_id=run.tenant_id,user_id=run.user_id)):
                result=await session.execute(text("""
                  UPDATE run_input_requests SET ready=true WHERE id=:id AND run_id=:run AND status='pending'
                  RETURNING question,version,expires_at
                """),dict(id=question_id,run=run.id))
                row=result.mappings().first()
                if row:
                    event=await self._runs.append_event(session,run_id=run.id,tenant_id=run.tenant_id,
                        event_type="input_required",payload={"question_id":str(question_id),"version":row["version"],
                        "question":row["question"],"expires_at":row["expires_at"].isoformat(),"status":"waiting_input"})
                    if self._events:
                        self._events.publish_after_commit(event)

    async def claim(self,run_id,question_id):
        """原子领取已回答问题，重复 Redis 消息不能再次恢复。"""
        async with self._database.session() as session:
            async with session.begin():
                result=await session.execute(text("""
                  UPDATE agent_runs ar SET status='running',current_stage='处理补充回答',updated_at=now()
                  WHERE ar.id=:run AND ar.status='waiting_input'
                  AND EXISTS (SELECT 1 FROM run_input_requests q WHERE q.id=:id AND q.run_id=ar.id
                              AND q.tenant_id=ar.tenant_id AND q.user_id=ar.user_id
                              AND q.status='answered' AND q.ready=true AND q.expires_at>now())
                  RETURNING ar.id,ar.tenant_id,ar.user_id,ar.conversation_id,ar.run_type
                """),dict(run=run_id,id=question_id))
                row=result.mappings().first()
                if row is None:
                    return None,None
                run=ClaimedRun(**row)
                await session.execute(text("SELECT set_config('app.tenant_id',:tenant,true),set_config('app.user_id',:user,true)"),
                                      dict(tenant=str(run.tenant_id),user=str(run.user_id)))
                result=await session.execute(text("""
                  UPDATE run_input_requests SET status='consumed'
                  WHERE id=:id AND run_id=:run AND tenant_id=:tenant AND user_id=:user AND status='answered' RETURNING response
                """),dict(id=question_id,run=run_id,tenant=run.tenant_id,user=run.user_id))
                row=result.mappings().first()
                if row is None:
                    raise HarnessError("INPUT_CONFLICT","补充回答已失效")
                return run,{"question_id":str(question_id),"response":row["response"]}
