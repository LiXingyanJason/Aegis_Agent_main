"""用户补充请求和草稿修改任务的本地业务数据访问。"""
from sqlalchemy import text
from uuid import uuid4
import json
from app.event.run_event import RunEvent


class AgentInputRepository:
    async def notice(self,session,user,run_id,kind,payload):
        await session.execute(text("SELECT id FROM agent_runs WHERE id=:run AND tenant_id=:tenant AND user_id=:user FOR UPDATE"),
                              dict(run=run_id,tenant=user.tenant_id,user=user.id))
        result=await session.execute(text("""
          INSERT INTO run_events(tenant_id,run_id,event_no,event_type,payload)
          SELECT :tenant,:run,COALESCE(MAX(event_no),0)+1,:kind,CAST(:payload AS jsonb)
          FROM run_events WHERE run_id=:run RETURNING event_no,created_at
        """),dict(tenant=user.tenant_id,run=run_id,kind=kind,payload=json.dumps(payload,ensure_ascii=False)))
        row=result.mappings().one()
        return RunEvent(run_id,row["event_no"],kind,payload,row["created_at"])

    async def list_inputs(self, session, user, run_id=None):
        events=[]
        # 按当前用户惰性清理已过期等待，读取时不会恢复或执行任何工具。
        expired=await session.execute(text("""
          UPDATE run_input_requests q SET status='expired'
          WHERE q.tenant_id=:tenant AND q.user_id=:user AND q.status IN ('pending','answered') AND q.expires_at<=now()
            AND (CAST(:run AS uuid) IS NULL OR q.run_id=CAST(:run AS uuid))
          RETURNING run_id
        """),dict(run=run_id,tenant=user.tenant_id,user=user.id))
        for row in expired.mappings().all():
            updated=await session.execute(text("""
              UPDATE agent_runs SET status='failed',error_code='INPUT_EXPIRED',
                     error_message='补充问题已过期，请重新起草',finished_at=now(),updated_at=now()
              WHERE id=:run AND tenant_id=:tenant AND user_id=:user AND status='waiting_input'
              RETURNING id
            """),dict(run=row["run_id"],tenant=user.tenant_id,user=user.id))
            await session.execute(text("UPDATE mail_reply_draft_requests SET status='failed' WHERE run_id=:run AND tenant_id=:tenant"),
                                  dict(run=row["run_id"],tenant=user.tenant_id))
            if updated.mappings().first() is not None:
                events.append(await self.notice(session,user,row["run_id"],"run_failed",
                                               {"status":"failed","error_code":"INPUT_EXPIRED","error_message":"补充问题已过期，请重新起草"}))
        result = await session.execute(text("""
          SELECT q.id AS question_id,q.run_id,q.question,q.version,q.status,q.ready,q.expires_at,
                 q.expires_at<=now() AS expired,association.cooperation,q.input_type,
                 (SELECT COALESCE(jsonb_agg(jsonb_build_object('id',item->>'id','label',item->>'label') ORDER BY ord),'[]'::jsonb)
                  FROM jsonb_array_elements(q.options) WITH ORDINALITY AS opt(item,ord)) AS options
          FROM run_input_requests q LEFT JOIN LATERAL (
            SELECT e.payload->'cooperation' AS cooperation FROM run_events e
            WHERE e.run_id=q.run_id AND e.tenant_id=q.tenant_id AND e.event_type='input_required'
              AND e.payload->>'question_id'=q.id::text ORDER BY e.event_no DESC LIMIT 1
          ) association ON true
          WHERE (CAST(:run AS uuid) IS NULL OR q.run_id=CAST(:run AS uuid)) AND q.tenant_id=:tenant AND q.user_id=:user
          ORDER BY q.version
        """),dict(run=run_id,tenant=user.tenant_id,user=user.id))
        items=[]
        for row in result.mappings().all():
            item=dict(row)
            if isinstance(item.get("options"),str): item["options"]=json.loads(item["options"])
            items.append(item)
        return items,events

    async def answer(self, session, user, question_id, param):
        result = await session.execute(text("""
          SELECT q.*, ar.status AS run_status, q.expires_at<=now() AS expired
          FROM run_input_requests q JOIN agent_runs ar ON ar.id=q.run_id AND ar.tenant_id=q.tenant_id AND ar.user_id=q.user_id
          WHERE q.id=:id AND q.tenant_id=:tenant AND q.user_id=:user FOR UPDATE OF q,ar
        """),dict(id=question_id,tenant=user.tenant_id,user=user.id))
        q = result.mappings().first()
        if q is None:
            return None
        kind=q.get("input_type","text")
        if kind=="single_choice":
            if param.selected_option_id is None or param.response is not None: return "conflict"
            options=q.get("options",[])
            if isinstance(options,str): options=json.loads(options)
            matches=[item for item in options if item.get("id")==param.selected_option_id]
            if len(matches)!=1: return "conflict"
            ref=matches[0].get("reference",{})
            if ref.get("resource_type")!="email" or not ref.get("source_version"): return "conflict"
            valid=await session.execute(text("""
              SELECT em.id FROM email_messages em JOIN provider_connections pc ON pc.id=em.connection_id
                AND pc.tenant_id=em.tenant_id AND pc.user_id=em.user_id
              WHERE em.id=CAST(:email AS uuid) AND em.tenant_id=:tenant AND em.user_id=:user
                AND em.source_version=:version AND pc.status='active'
                AND (pc.expires_at IS NULL OR pc.expires_at>now()) AND pc.provider IN ('gmail','outlook_mail')
                AND pc.scopes @> '["mail.read"]'::jsonb
            """),dict(email=ref["id"],version=ref["source_version"],tenant=user.tenant_id,user=user.id))
            if valid.mappings().first() is None: return "conflict"
        elif kind!="text" or param.response is None or param.selected_option_id is not None:
            return "conflict"
        if q["status"] in ("answered","consumed") and q["client_response_id"] == param.client_response_id and q["response"] == param.response and q.get("selected_option_id") == param.selected_option_id and q["version"] == param.expected_version:
            return {"run_id":q["run_id"],"question_id":question_id,
                    "execution_scheduled":q["status"]=="answered" and q["run_status"]=="waiting_input" and not q["expired"],
                    "idempotent_replay":True}
        if q["expired"] or q["version"] != param.expected_version or not q["ready"] or q["status"] != "pending" or q["run_status"] != "waiting_input":
            return "conflict"
        await session.execute(text("UPDATE run_input_requests SET status='answered',response=:response,selected_option_id=:selected,client_response_id=:client WHERE id=:id"),
                              dict(id=question_id,response=param.response,selected=param.selected_option_id,client=param.client_response_id))
        return {"run_id":q["run_id"],"question_id":question_id,"execution_scheduled":True,"idempotent_replay":False}

    async def cancel(self, session, user, question_id):
        result = await session.execute(text("""
          SELECT q.run_id,q.status,ar.status AS run_status FROM run_input_requests q
          JOIN agent_runs ar ON ar.id=q.run_id
          WHERE q.id=:id AND q.tenant_id=:tenant AND q.user_id=:user FOR UPDATE OF q,ar
        """),dict(id=question_id,tenant=user.tenant_id,user=user.id))
        row = result.mappings().first()
        if row is None:
            return None
        if row["status"] != "pending" or row["run_status"] != "waiting_input":
            return "conflict"
        await session.execute(text("UPDATE run_input_requests SET status='cancelled' WHERE id=:id"),dict(id=question_id))
        await session.execute(text("UPDATE agent_runs SET status='cancelled',finished_at=now(),current_stage='用户已取消',updated_at=now() WHERE id=:run"),
                              dict(run=row["run_id"]))
        await session.execute(text("UPDATE mail_reply_draft_requests SET status='failed' WHERE run_id=:run AND tenant_id=:tenant"),
                              dict(run=row["run_id"],tenant=user.tenant_id))
        event=await self.notice(session,user,row["run_id"],"run_cancelled",{"status":"cancelled"})
        return {"run_id":row["run_id"],"status":"cancelled","event":event}

    async def create_revision(self, session, user, draft_id, param):
        result = await session.execute(text("""
          SELECT id,version,status FROM mail_drafts WHERE id=:id AND tenant_id=:tenant AND user_id=:user FOR UPDATE
        """),dict(id=draft_id,tenant=user.tenant_id,user=user.id))
        draft = result.mappings().first()
        if draft is None:
            return None
        args=dict(draft=draft_id,tenant=user.tenant_id,user=user.id,client=param.client_request_id)
        replay = await session.execute(text("""
          SELECT r.run_id,r.expected_version,r.instruction,ar.status
          FROM mail_draft_revision_requests r JOIN agent_runs ar ON ar.id=r.run_id
          WHERE r.draft_id=:draft AND r.tenant_id=:tenant AND r.user_id=:user AND r.client_request_id=:client
        """),args)
        row = replay.mappings().first()
        if row:
            if row["expected_version"] != param.expected_version or row["instruction"] != param.instruction:
                return "conflict"
            return {"run_id":row["run_id"],"status":row["status"],"idempotent_replay":True}
        if draft["status"] != "draft" or draft["version"] != param.expected_version:
            return "conflict"
        result = await session.execute(text("""
          INSERT INTO agent_runs(tenant_id,user_id,run_type,status,current_stage,request_id)
          VALUES(:tenant,:user,'mail_draft_revision','queued','等待修改草稿',:request_id) RETURNING id
        """),{**args,"request_id":uuid4().hex})
        run_id=result.mappings().one()["id"]
        args.update(run=run_id,expected=param.expected_version,instruction=param.instruction)
        await session.execute(text("""
          INSERT INTO mail_draft_revision_requests(tenant_id,user_id,run_id,draft_id,expected_version,instruction,client_request_id)
          VALUES(:tenant,:user,:run,:draft,:expected,:instruction,:client)
        """),args)
        return {"run_id":run_id,"status":"queued","idempotent_replay":False}
