"""草稿正文修改的乐观版本保护。"""
from sqlalchemy import text
from aegis_agent_worker.harness.exceptions import HarnessError


class MailRevisionRepository:
    async def load(self,session,run):
        result=await session.execute(text("""
          SELECT d.id,d.body,d.subject,d.version,d.status,r.expected_version,r.instruction
          FROM mail_draft_revision_requests r JOIN mail_drafts d ON d.id=r.draft_id
          WHERE r.run_id=:run AND r.tenant_id=:tenant AND r.user_id=:user
            AND d.tenant_id=:tenant AND d.user_id=:user
        """),dict(run=run.id,tenant=run.tenant_id,user=run.user_id))
        row=result.mappings().first()
        if row is None or row["status"]!="draft" or row["version"]!=row["expected_version"]:
            raise HarnessError("DRAFT_VERSION_CONFLICT","草稿已修改或状态不允许AI修改")
        return dict(row)

    async def save(self,session,run,draft,body):
        result=await session.execute(text("""
          UPDATE mail_drafts SET body=:body,version=version+1,updated_at=now()
          WHERE id=:id AND tenant_id=:tenant AND user_id=:user AND status='draft' AND version=:version
          RETURNING id,version
        """),dict(id=draft["id"],tenant=run.tenant_id,user=run.user_id,version=draft["version"],body=body))
        row=result.mappings().first()
        if row is None:
            raise HarnessError("DRAFT_VERSION_CONFLICT","草稿版本已改变，AI结果未覆盖")
        await session.execute(text("UPDATE mail_draft_revision_requests SET status='succeeded' WHERE run_id=:run AND tenant_id=:tenant"),
                              dict(run=run.id,tenant=run.tenant_id))
        await session.execute(text("UPDATE agent_runs SET status='completed',current_stage='草稿修改完成',finished_at=now(),updated_at=now() WHERE id=:run AND tenant_id=:tenant"),
                              dict(run=run.id,tenant=run.tenant_id))
        return dict(row)
