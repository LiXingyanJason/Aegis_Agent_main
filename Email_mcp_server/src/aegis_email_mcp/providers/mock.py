"""以固定 JSON 文件保存开发邮件数据的 Mock Provider。"""

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from aegis_email_mcp.schemas.email import (
    AttachmentVO,
    EmailMessage,
    EmailRequestContext,
    EmailSendParam,
    SentEmailVO,
)


class MockEmailProvider:
    """读取收件箱 JSON，并将已发送邮件写入独立的 Mock JSON 文件。"""

    def __init__(self, data_path: Path, sent_data_path: Path | None = None) -> None:
        self._data_path = data_path
        self._sent_data_path = sent_data_path or data_path.with_name("mock_sent_mailbox.json")
        self._send_lock = asyncio.Lock()

    async def list_messages(self, context: EmailRequestContext) -> list[EmailMessage]:
        """读取全部 Mock 邮件，并按接收时间倒序返回。"""
        del context
        messages = [_message_from_record(item) for item in self._load()]
        return sorted(messages, key=lambda item: item.received_at, reverse=True)

    async def get_message(
        self, context: EmailRequestContext, provider_message_id: str
    ) -> EmailMessage | None:
        """读取指定 Mock 邮件；不存在时返回 None。"""
        del context
        for item in self._load():
            if item.get("provider_message_id") == provider_message_id:
                return _message_from_record(item)
        return None

    async def send_message(
        self, context: EmailRequestContext, param: EmailSendParam
    ) -> SentEmailVO:
        """写入 Mock 已发送箱；同一幂等键重复调用返回首次回执。"""
        del context
        async with self._send_lock:
            records = self._load_sent()
            for record in records:
                if record.get("idempotency_key") == param.idempotency_key:
                    return _sent_from_record(record)
            receipt = {
                "provider_message_id": f"mock-sent-{uuid4().hex}",
                "sent_at": datetime.now(UTC).isoformat(),
                "status": "sent",
                "recipients": {"to": param.to, "cc": param.cc},
                "subject": param.subject,
                "body": param.body,
                "idempotency_key": param.idempotency_key,
            }
            records.append(receipt)
            self._save_sent(records)
            return _sent_from_record(receipt)

    def _load(self) -> list[dict[str, Any]]:
        """读取并校验固定 JSON 邮箱数据。"""
        try:
            payload = json.loads(self._data_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"Mock 邮箱数据文件不可读取：{self._data_path}") from error
        if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
            raise ValueError("Mock 邮箱数据必须是邮件对象数组")
        return payload

    def _load_sent(self) -> list[dict[str, Any]]:
        """读取 Mock 已发送箱；首次发送时允许文件尚不存在。"""
        if not self._sent_data_path.exists():
            return []
        try:
            payload = json.loads(self._sent_data_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"Mock 已发送箱文件不可读取：{self._sent_data_path}") from error
        if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
            raise ValueError("Mock 已发送箱必须是邮件对象数组")
        return payload

    def _save_sent(self, records: list[dict[str, Any]]) -> None:
        """原子替换 Mock 已发送箱，避免进程中断留下半份 JSON。"""
        self._sent_data_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self._sent_data_path.with_suffix(".tmp")
        temporary_path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary_path.replace(self._sent_data_path)


def _message_from_record(item: dict[str, Any]) -> EmailMessage:
    """将 JSON 记录转换为标准化邮件领域对象。"""
    try:
        attachments = tuple(AttachmentVO.model_validate(value) for value in item.get("attachments", []))
        return EmailMessage(
            provider_message_id=str(item["provider_message_id"]),
            provider_thread_id=_optional_string(item.get("provider_thread_id")),
            sender_name=_optional_string(item.get("sender_name")),
            sender_email=str(item["sender_email"]),
            recipients=tuple(str(value) for value in item.get("recipients", [])),
            subject=str(item["subject"]),
            received_at=datetime.fromisoformat(str(item["received_at"])),
            source_version=str(item["source_version"]),
            list_preview=str(item["list_preview"]),
            body_text=str(item["body_text"]),
            attachments=attachments,
        )
    except (KeyError, TypeError, ValueError, ValidationError) as error:
        raise ValueError("Mock 邮件记录格式无效") from error


def _optional_string(value: object) -> str | None:
    """将可空 JSON 字段规范为字符串或 None。"""
    return None if value is None else str(value)


def _sent_from_record(record: dict[str, Any]) -> SentEmailVO:
    """将 Mock 已发送记录转换为不含正文的发送回执。"""
    try:
        recipients = record["recipients"]
        if not isinstance(recipients, dict):
            raise ValueError("recipients 格式无效")
        return SentEmailVO(
            provider_message_id=str(record["provider_message_id"]),
            sent_at=datetime.fromisoformat(str(record["sent_at"])),
            status=str(record.get("status", "sent")),
            recipients={
                "to": [str(value) for value in recipients.get("to", [])],
                "cc": [str(value) for value in recipients.get("cc", [])],
            },
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("Mock 已发送邮件记录格式无效") from error
