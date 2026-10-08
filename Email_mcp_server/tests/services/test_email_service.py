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
        assert len(messages.messages) == 3
        assert "body_text" not in messages.messages[0].model_dump()
        assert "2026-10-09" in detail.body_text
        assert detail.attachments[0].file_name == "renewal-proposal.pdf"

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
