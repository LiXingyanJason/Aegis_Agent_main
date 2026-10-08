"""邮件管理的应用服务。"""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.integration.email_mcp_client import EmailMCPError, EmailMCPMessage, EmailMCPMessageDetail
from app.repository.mail_repository import MailRepository
from app.repository.user_repository import LocalUser


class MailConnectionRequiredError(RuntimeError):
    """用户没有可用邮箱读取连接时抛出。"""


class MailToolUnavailableError(RuntimeError):
    """内部邮件工具服务不可用时抛出。"""


class MailListClient(Protocol):
    """便于测试替换 Email MCP Client 的最小接口。"""

    async def list_messages(
        self, *, connection_id, tenant_id, user_id
    ) -> list[EmailMCPMessage]: ...

    async def get_message(
        self, *, connection_id, tenant_id, user_id, provider_message_id
    ) -> EmailMCPMessageDetail: ...


@dataclass(frozen=True, slots=True)
class MailListSnapshot:
    """同步后返回给 Controller 的邮件列表和快照时刻。"""

    items: list[dict]
    snapshot_at: datetime | None


@dataclass(frozen=True, slots=True)
class MailAsyncRequest:
    """提交邮件后台任务后返回的运行与可选缓存结果。"""

    status: str
    run_id: UUID | None = None
    extraction_id: UUID | None = None
    cached: bool = False
    extraction: dict | None = None


class MailService:
    """协调邮箱连接、Email MCP 读取和 Aegis 邮件快照存储。"""

    def __init__(
        self,
        client: MailListClient,
        repository: MailRepository | None = None,
    ) -> None:
        self._client = client
        self._repository = repository or MailRepository()

    async def sync_and_list_messages(
        self, session: AsyncSession, local_user: LocalUser
    ) -> MailListSnapshot:
        """同步当前用户全部邮件元数据，并读取本地快照供页面展示。"""
        connection = await self._repository.find_active_read_connection(
            session, tenant_id=local_user.tenant_id, user_id=local_user.id
        )
        if connection is None:
            raise MailConnectionRequiredError("尚未连接具有邮件读取权限的工作邮箱")
        try:
            messages = await self._client.list_messages(
                connection_id=connection.id,
                tenant_id=local_user.tenant_id,
                user_id=local_user.id,
            )
        except EmailMCPError as error:
            raise MailToolUnavailableError(str(error)) from error
        await self._repository.upsert_messages(
            session,
            tenant_id=local_user.tenant_id,
            user_id=local_user.id,
            connection_id=connection.id,
            messages=messages,
        )
        return MailListSnapshot(
            items=await self._repository.list_messages(
                session,
                tenant_id=local_user.tenant_id,
                user_id=local_user.id,
                connection_id=connection.id,
            ),
            snapshot_at=await self._repository.latest_snapshot_at(
                session,
                tenant_id=local_user.tenant_id,
                user_id=local_user.id,
                connection_id=connection.id,
            ),
        )

    async def get_message_detail(
        self, session: AsyncSession, local_user: LocalUser, email_id: UUID
    ) -> EmailMCPMessageDetail | None:
        """校验快照归属后按需读取正文；正文不写回默认邮件快照。"""
        connection = await self._repository.find_active_read_connection(
            session, tenant_id=local_user.tenant_id, user_id=local_user.id
        )
        if connection is None:
            raise MailConnectionRequiredError("尚未连接具有邮件读取权限的工作邮箱")
        reference = await self._repository.find_owned_message_detail_reference(
            session,
            tenant_id=local_user.tenant_id,
            user_id=local_user.id,
            email_id=email_id,
            connection_id=connection.id,
        )
        if reference is None:
            return None
        try:
            return await self._client.get_message(
                connection_id=connection.id,
                tenant_id=local_user.tenant_id,
                user_id=local_user.id,
                provider_message_id=reference["provider_message_id"],
            )
        except EmailMCPError as error:
            raise MailToolUnavailableError(str(error)) from error

    async def list_drafts(self, session: AsyncSession, local_user: LocalUser) -> list[dict]:
        """返回当前用户可继续编辑或待确认的站内草稿列表。"""
        return await self._repository.list_drafts(
            session, tenant_id=local_user.tenant_id, user_id=local_user.id
        )

    async def get_draft(
        self, session: AsyncSession, local_user: LocalUser, draft_id
    ) -> dict | None:
        """读取当前用户的一封完整站内草稿。"""
        return await self._repository.find_draft(
            session,
            tenant_id=local_user.tenant_id,
            user_id=local_user.id,
            draft_id=draft_id,
        )

    async def update_draft(self, session: AsyncSession, local_user: LocalUser, draft_id, param):
        """保存草稿编辑；仓储会递增版本并作废旧待确认发送项。"""
        return await self._repository.update_draft(
            session,
            tenant_id=local_user.tenant_id,
            user_id=local_user.id,
            draft_id=draft_id,
            to=param.to,
            cc=param.cc,
            subject=param.subject,
            body=param.body,
        )

    async def request_send_confirmation(self, session: AsyncSession, local_user: LocalUser, draft_id):
        """将已保存草稿冻结为待确认发送操作；此时仍不会向外发送邮件。"""
        return await self._repository.create_send_confirmation(
            session, tenant_id=local_user.tenant_id, user_id=local_user.id, draft_id=draft_id
        )

    async def list_sent_messages(self, session: AsyncSession, local_user: LocalUser) -> list[dict]:
        """读取当前用户的已发送记录。"""
        return await self._repository.list_sent_messages(
            session, tenant_id=local_user.tenant_id, user_id=local_user.id
        )

    async def create_todo_drafts(self, session: AsyncSession, local_user: LocalUser, email_id, param):
        """复制已有提取结果的待办候选，不调用 Email MCP 或 LLM。"""
        return await self._repository.create_todo_drafts(
            session,
            tenant_id=local_user.tenant_id,
            user_id=local_user.id,
            email_id=email_id,
            extraction_id=param.extraction_id,
            todo_indexes=param.todo_indexes,
        )

    async def request_extraction(self, session: AsyncSession, local_user: LocalUser, email_id, param):
        """复用同版本成功摘要，或创建新的异步邮件提取任务。"""
        email = await self._repository.find_owned_email(
            session, tenant_id=local_user.tenant_id, user_id=local_user.id, email_id=email_id
        )
        if email is None:
            return None
        if not param.refresh:
            cached = await self._repository.find_reusable_extraction(
                session,
                tenant_id=local_user.tenant_id,
                user_id=local_user.id,
                email_id=email_id,
                source_version=email["source_version"],
                extractor_version="mail-extract-v1",
            )
            if cached is not None:
                todos = await self._repository.list_extraction_todos(
                    session, tenant_id=local_user.tenant_id, extraction_id=cached["id"]
                )
                return MailAsyncRequest(
                    status="completed",
                    extraction_id=cached["id"],
                    cached=True,
                    extraction={
                        "key_points": cached["key_points"] or [],
                        "due_date_candidates": cached["due_date_candidates"] or [],
                        "todos": [
                            {
                                "item_index": item["item_index"],
                                "content": item["todo_text"],
                                "due_at": item["due_at"],
                                "due_text": item["due_text"],
                                "is_inferred": item["is_inferred"],
                                "status": item["status"],
                            }
                            for item in todos
                        ],
                    },
                )
        run_id, extraction_id = await self._repository.create_extraction_run(
            session, tenant_id=local_user.tenant_id, user_id=local_user.id, email=email
        )
        return MailAsyncRequest(status="queued", run_id=run_id, extraction_id=extraction_id)

    async def request_reply_draft(self, session: AsyncSession, local_user: LocalUser, email_id, param):
        """创建独立的回复草稿任务；允许可选的已完成摘要作为上下文。"""
        email = await self._repository.find_owned_email(
            session, tenant_id=local_user.tenant_id, user_id=local_user.id, email_id=email_id
        )
        if email is None:
            return None
        run_id = await self._repository.create_reply_draft_run(
            session,
            tenant_id=local_user.tenant_id,
            user_id=local_user.id,
            email=email,
            extraction_id=param.extraction_id,
            instruction=param.instruction,
        )
        return None if run_id is None else MailAsyncRequest(status="queued", run_id=run_id)
