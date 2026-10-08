"""以固定 JSON 文件保存开发邮件数据的 Mock Provider。"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from aegis_email_mcp.schemas.email import AttachmentVO, EmailMessage, EmailRequestContext


class MockEmailProvider:
    """读取固定 JSON 邮箱；一期不提供新增、修改或发送能力。"""

    def __init__(self, data_path: Path) -> None:
        self._data_path = data_path

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

    def _load(self) -> list[dict[str, Any]]:
        """读取并校验固定 JSON 邮箱数据。"""
        try:
            payload = json.loads(self._data_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"Mock 邮箱数据文件不可读取：{self._data_path}") from error
        if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
            raise ValueError("Mock 邮箱数据必须是邮件对象数组")
        return payload


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
