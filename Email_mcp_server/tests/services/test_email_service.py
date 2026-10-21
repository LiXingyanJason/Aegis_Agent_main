"""邮件领域服务测试。"""

import asyncio
from pathlib import Path
from uuid import UUID

from aegis_email_mcp.providers.mock import MockEmailProvider
from aegis_email_mcp.schemas.email import EmailRequestContext, EmailSendParam
from aegis_email_mcp.services.email_service import EmailService


def _context() -> EmailRequestContext:
    """构造一份由主后端传入的最小 MCP 上下文。"""
    return EmailRequestContext(
        connection_id=UUID("10000000-0000-4000-8000-000000000021"),
        tenant_id=UUID("10000000-0000-4000-8000-000000000001"),
        user_id=UUID("10000000-0000-4000-8000-000000000010"),
        run_id=UUID("2dde4c59-6667-4af5-b12d-0f0c5cbcc2a6"),
    )


def test_email_service_lists_metadata_and_returns_detail_body() -> None:
    """测试列表不会携带正文，而单封读取可以取得正文和附件元数据。"""

    async def verify() -> None:
        service = EmailService(MockEmailProvider(Path("data/mock_mailbox.json")))
        messages = await service.list_messages(_context())
        detail = await service.get_message(_context(), "mock-mail-renewal-001")
        assert len(messages.messages) == 7
        assert "body_text" not in messages.messages[0].model_dump()
        assert "2026-10-09" in detail.body_text
        assert detail.attachments[0].file_name == "renewal-proposal.pdf"

    asyncio.run(verify())


def test_cooperation_fixtures_offer_distinct_choices_and_missing_information() -> None:
    """同类续约会议有多个真实候选；完整/缺信息样例不串用客户条件。"""
    async def verify() -> None:
        service = EmailService(MockEmailProvider(Path("data/mock_mailbox.json")))
        listed = (await service.list_messages(_context())).messages
        ids = [item.provider_message_id for item in listed]
        assert len(ids) == len(set(ids))
        assert [item.received_at for item in listed] == sorted((item.received_at for item in listed), reverse=True)
        choices = [item for item in listed if "续约会议" in item.subject]
        assert len(choices) == 4
        assert all("body_text" not in item.model_dump() for item in choices)
        a = await service.get_message(_context(), "mock-mail-renewal-meeting-004")
        b = await service.get_message(_context(), "mock-mail-renewal-meeting-005")
        c = await service.get_message(_context(), "mock-mail-renewal-meeting-006")
        d = await service.get_message(_context(), "mock-mail-renewal-meeting-007")
        assert "2026-10-26" in a.body_text and "50分钟" in a.body_text
        assert "2026-10-27" in b.body_text and "30分钟" in b.body_text
        assert "2026-10-28" in c.body_text and "时长尚待确定" in c.list_preview
        assert "45分钟" in d.body_text and "不是会议日期" in d.body_text
    asyncio.run(verify())


def test_email_service_sends_idempotently_to_mock_sent_mailbox(tmp_path: Path) -> None:
    """测试发送工具写入独立已发送箱，重复幂等键不会创建第二封邮件。"""

    async def verify() -> None:
        provider = MockEmailProvider(
            Path("data/mock_mailbox.json"), tmp_path / "mock_sent_mailbox.json"
        )
        service = EmailService(provider)
        param = EmailSendParam(
            to=["recipient@example.com"], subject="测试发送", body="这是测试正文",
            idempotency_key="test-mail-send-001",
        )
        first = await service.send_message(_context(), param)
        second = await service.send_message(_context(), param)
        assert first.provider_message_id == second.provider_message_id
        assert first.recipients == {"to": ["recipient@example.com"], "cc": []}

    asyncio.run(verify())
