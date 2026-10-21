"""补充问题的业务存储与同一 run 恢复，问题不是审批决定。"""
from sqlalchemy import text
import json
from aegis_agent_worker.agent.contracts.input_choice import validate_options,public_options,choice_id
from aegis_agent_worker.config.database import TenantContext, tenant_transaction
from aegis_agent_worker.repository.run_repository import ClaimedRun
from aegis_agent_worker.harness.exceptions import HarnessError


class InputRequestService:
    def __init__(self,database,runs,events=None):
        self._database,self._runs,self._events=database,runs,events

    async def prepare(self,run,question,*,request_id=None,input_type="text",options=()):
        options=validate_options(input_type,options)
        if input_type=="single_choice" and (request_id is None or any(
                item['id']!=choice_id(request_id,item['reference']) for item in options)):
            raise ValueError("选择项ID必须由当前问题和来源版本派生")
        def output(id,version):
            return dict(question_id=str(id),version=version,question=question,input_type=input_type,options=public_options(options))
        async with self._database.session() as session:
            async with tenant_transaction(session,TenantContext(tenant_id=run.tenant_id,user_id=run.user_id)):
                await session.execute(text("SELECT id FROM agent_runs WHERE id=:id FOR UPDATE"),dict(id=run.id))
                if request_id is not None:
                    # 同一专家委派/轮次的准备节点重入时复用原问题，无需新增表字段。
                    from uuid import UUID
                    request_id = UUID(str(request_id))
                    existing = await session.execute(text("""
                      SELECT id,version,question,status,input_type,options,expires_at>now() AS valid
                      FROM run_input_requests
                      WHERE id=:id AND run_id=:run AND tenant_id=:tenant AND user_id=:user
                    """),dict(id=request_id,run=run.id,tenant=run.tenant_id,user=run.user_id))
                    previous = existing.mappings().first()
                    if previous is not None:
                        stored=previous.get('options',[])
                        if isinstance(stored,str): stored=json.loads(stored)
                        if (previous['status'] != 'pending' or not previous['valid'] or previous['question'] != question
                            or previous.get('input_type','text')!=input_type or stored!=options):
                            raise HarnessError('INPUT_CONFLICT','原补充问题已失效或已被处理')
                        await session.execute(text("UPDATE agent_runs SET status='waiting_input',current_stage='等待补充信息',updated_at=now() WHERE id=:id"),
                                              dict(id=run.id))
                        return output(previous['id'],previous['version'])
                    result=await session.execute(text("""
                      INSERT INTO run_input_requests(id,tenant_id,user_id,run_id,question,version,input_type,options)
                      SELECT :id,:tenant,:user,:run,:question,COALESCE(MAX(version),0)+1,:input_type,CAST(:options AS jsonb)
                      FROM run_input_requests WHERE run_id=:run RETURNING id,version
                    """),dict(id=request_id,tenant=run.tenant_id,user=run.user_id,run=run.id,question=question,input_type=input_type,options=json.dumps(options,ensure_ascii=False)))
                else:
                    result=await session.execute(text("""
                      INSERT INTO run_input_requests(tenant_id,user_id,run_id,question,version,input_type,options)
                      SELECT :tenant,:user,:run,:question,COALESCE(MAX(version),0)+1,:input_type,CAST(:options AS jsonb)
                      FROM run_input_requests WHERE run_id=:run RETURNING id,version
                    """),dict(tenant=run.tenant_id,user=run.user_id,run=run.id,question=question,input_type=input_type,options=json.dumps(options,ensure_ascii=False)))
                row=result.mappings().one()
                await session.execute(text("UPDATE agent_runs SET status='waiting_input',current_stage='等待补充信息',updated_at=now() WHERE id=:id"),
                                      dict(id=run.id))
                return output(row["id"],row["version"])

    async def mark_ready(self,run,question_id,*,cooperation=None):
        """ainvoke 返回 interrupt 后再发布问题，此时 Checkpointer 已完成保存。"""
        if cooperation is not None:
            from aegis_agent_worker.event.cooperation_context import validate_association
            cooperation=validate_association(run.id,cooperation)
        async with self._database.session() as session:
            async with tenant_transaction(session,TenantContext(tenant_id=run.tenant_id,user_id=run.user_id)):
                result=await session.execute(text("""
                  UPDATE run_input_requests SET ready=true WHERE id=:id AND run_id=:run AND status='pending' AND ready=false
                    AND tenant_id=:tenant AND user_id=:user
                    AND EXISTS(SELECT 1 FROM agent_runs ar WHERE ar.id=:run AND ar.tenant_id=:tenant
                      AND ar.user_id=:user AND ar.status='waiting_input')
                  RETURNING question,version,expires_at,input_type,options
                """),dict(id=question_id,run=run.id,tenant=run.tenant_id,user=run.user_id))
                row=result.mappings().first()
                if row:
                    payload={"question_id":str(question_id),"version":row["version"],
                        "question":row["question"],"expires_at":row["expires_at"].isoformat(),"status":"waiting_input"}
                    items=row.get("options",[])
                    if isinstance(items,str): items=json.loads(items)
                    items=validate_options(row.get("input_type","text"),items)
                    payload.update(input_type=row.get("input_type","text"),options=public_options(items))
                    if cooperation is not None: payload["cooperation"]=cooperation
                    event=await self._runs.append_event(session,run_id=run.id,tenant_id=run.tenant_id,
                        event_type="input_required",payload=payload)
                    if self._events:
                        self._events.publish_after_commit(event)
                    if cooperation is not None:
                        from aegis_agent_worker.service.cooperation.team_event_service import TeamEventService
                        # 展示关联来自已保存中断，不参与权限或审批判断。
                        await TeamEventService(self._database,self._runs,self._events).record(run,phase="waiting_input",
                            round_no=cooperation["round"],policy_version=await self._policy_version(session,run),
                            association=cooperation,question_id=question_id,session=session)

    async def _policy_version(self,session,run):
        result=await session.execute(text("SELECT policy_version FROM run_execution_budgets WHERE run_id=:run AND tenant_id=:tenant AND user_id=:user"),
            dict(run=run.id,tenant=run.tenant_id,user=run.user_id))
        row=result.mappings().first()
        if row is None:
            raise HarnessError("EXECUTION_STATE_UNAVAILABLE","无法核验补问任务策略")
        return row["policy_version"]

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
                  WHERE id=:id AND run_id=:run AND tenant_id=:tenant AND user_id=:user AND status='answered'
                  RETURNING response,input_type,options,selected_option_id
                """),dict(id=question_id,run=run_id,tenant=run.tenant_id,user=run.user_id))
                row=result.mappings().first()
                if row is None:
                    raise HarnessError("INPUT_CONFLICT","补充回答已失效")
                if row.get("input_type","text")=="single_choice":
                    items=row["options"]
                    if isinstance(items,str): items=json.loads(items)
                    items=validate_options("single_choice",items)
                    matches=[item for item in items if item['id']==row['selected_option_id']]
                    if len(matches)!=1 or choice_id(question_id,matches[0]['reference'])!=row['selected_option_id']:
                        raise HarnessError("INPUT_CONFLICT","选项快照不一致")
                    return run,{"question_id":str(question_id),"selected_option_id":row['selected_option_id']}
                return run,{"question_id":str(question_id),"response":row["response"]}
