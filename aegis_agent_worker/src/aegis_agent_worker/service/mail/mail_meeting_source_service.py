"""会议要求业务来源：查询复用现有服务，正文读取复用Gateway审计，不调用模型。"""
from pydantic import ValidationError

from aegis_agent_worker.agent.contracts.cooperation.messages import ExpertResult
from aegis_agent_worker.agent.contracts.decision import ResourceReference
from aegis_agent_worker.agent.contracts.mail import MailListMessage
from aegis_agent_worker.config.database import TenantContext, tenant_transaction
from aegis_agent_worker.repository.mail_meeting_repository import MailMeetingRepository
from aegis_agent_worker.tool.contracts import ToolError, ToolInvocation


class MailMeetingSourceService:
    def __init__(self, database, query_service, reader, repository=None):
        self._database, self._queries, self._reader = database, query_service, reader
        self._repository = repository or MailMeetingRepository()

    async def _source(self, run, reference):
        reference = ResourceReference.model_validate(reference)
        if reference.resource_type != "email" or not reference.source_version or reference.version is not None:
            raise ToolError("MAIL_REFERENCE_REQUIRED", "必须使用已核验的邮件来源引用")
        async with self._database.session() as session:
            async with tenant_transaction(session, TenantContext(run.tenant_id, run.user_id)):
                row = await self._repository.load_reference(session, run, reference)
        if row is None:
            raise ToolError("MAIL_SOURCE_CHANGED", "邮件不存在、版本已变化或连接读取权限已撤销")
        return row

    @staticmethod
    def candidate(row):
        # 无正文/列表预览/连接ID；模型使用站内ID，不选择外部账户。
        return {"reference": {"resource_type": "email", "id": str(row["id"]),
                               "source_version": row["source_version"]},
                "subject": row["subject"], "sender_email": row["sender_email"],
                "received_at": row["received_at"].isoformat()}

    async def describe(self, run, reference):
        return self.candidate(await self._source(run, reference))

    async def search(self, run, plan, limit):
        rows, _ = await self._queries.query(run, plan)
        return [self.candidate(row) for row in rows[:min(limit, plan.limit)]], len(rows)

    async def read(self, run, reference):
        source = await self._source(run, reference)
        raw = await self._reader.invoke(run, ToolInvocation("mail.messages.get",
            {"provider_message_id": source["provider_message_id"]}), source["connection_id"])
        try:
            metadata = MailListMessage.model_validate(raw)
        except ValidationError:
            raise ToolError("MAIL_INVALID_RESPONSE", "来源邮件响应无效") from None
        if (metadata.provider_message_id != source["provider_message_id"]
                or metadata.source_version != source["source_version"]
                or metadata.subject != source["subject"]
                or metadata.sender_email != source["sender_email"]):
            raise ToolError("MAIL_SOURCE_CHANGED", "来源邮件内容或版本已经变化")
        if not isinstance(raw.get("body_text"), str) or not raw["body_text"].strip():
            raise ToolError("MAIL_INVALID_RESPONSE", "邮件未提供可读取正文")
        # 正文仅返回给当前Workflow节点，调用方不得将此对象返回图State。
        return {**{key: raw.get(key) for key in ("sender_name", "sender_email", "subject", "body_text")},
                "received_at": metadata.received_at.isoformat(), "source_version": metadata.source_version}

    async def verify_result(self, run, delegation, result):
        result = ExpertResult.model_validate(result)
        if (delegation.target != "mail" or result.expert != "mail"
                or result.delegation_id != delegation.delegation_id or result.draft is not None):
            return False
        if result.status != "completed":
            if result.meeting_requirements is not None or len(result.references) > 1:
                return False
            try:
                for reference in result.references:
                    await self._source(run, reference)
            except ToolError:
                return False
            return True
        if result.meeting_requirements is None or len(result.references) != 1:
            return False
        try:
            await self._source(run, result.references[0])
        except ToolError:
            return False
        return True
