"""邮件专职 Agent：集中管理邮件摘要、回复起草的模型交互与结构化输出校验。"""

import json
from typing import Any

from aegis_agent_worker.llm.client import LLMMessage, OpenAICompatibleClient


class MailAgentError(RuntimeError):
    """邮件模型输出无法作为受控业务结果使用时抛出。"""


_EXTRACTION_SYSTEM_PROMPT = """你是邮件信息提取器。只输出 JSON，不要 Markdown。格式：
{"key_points":["不超过200字"],"todos":[{"content":"待办","due_at":"ISO 8601 或 null","due_text":"原文日期或 null","is_inferred":true}],"due_date_candidates":["ISO 8601"]}
不要编造事实；没有明确待办时 todos 返回空数组。"""

_REPLY_SYSTEM_PROMPT = """你是商务邮件起草助手。只输出 JSON，不要 Markdown。格式：
{"to":["email"],"cc":[],"subject":"主题","body":"正文"}
只起草，不声称已经发送。收件人应使用原邮件发件人或原邮件明确给出的地址。"""

class MailAgent:
    """只负责邮件语义推理，不读取数据库、不调用 MCP，也不持久化业务结果。"""

    def __init__(self, llm_client: OpenAICompatibleClient) -> None:
        self._llm_client = llm_client

    @property
    def provider_name(self) -> str:
        """返回本次邮件推理实际使用的模型供应商，供审计持久化。"""
        return self._llm_client.provider_name

    @property
    def model_name(self) -> str:
        """返回本次邮件推理实际使用的模型名称，供审计持久化。"""
        return self._llm_client.model_name

    async def generate_extraction(self, email: dict[str, Any]) -> dict[str, Any]:
        """针对一封已由工作流安全读取的邮件，生成摘要、待办和截止时间候选。"""
        raw = await self._llm_client.complete(
            [
                LLMMessage(role="system", content=_EXTRACTION_SYSTEM_PROMPT),
                LLMMessage(role="user", content=_mail_prompt(email)),
            ]
        )
        return _parse_extraction(raw)

    async def generate_reply_draft(
        self,
        email: dict[str, Any],
        *,
        key_points: Any,
        instruction: Any,
        memory_context: str | None = None,
    ) -> dict[str, Any]:
        """针对一封已安全读取的邮件生成站内回复草稿，绝不执行发送。"""
        messages = [LLMMessage(role="system", content=_REPLY_SYSTEM_PROMPT)]
        if memory_context:
            messages.append(LLMMessage(role="system", content=memory_context))
        messages.append(
            LLMMessage(
                role="user",
                content=_reply_prompt(email, key_points, instruction),
            )
        )
        return _parse_reply(await self._llm_client.complete(messages))




def _mail_prompt(email: dict[str, Any]) -> str:
    """仅向模型提供起草或摘要所需的来源邮件字段。"""
    return json.dumps(
        {
            key: email.get(key)
            for key in ("sender_name", "sender_email", "subject", "received_at", "body_text")
        },
        ensure_ascii=False,
    )


def _reply_prompt(email: dict[str, Any], key_points: Any, instruction: Any) -> str:
    """构造邮件回复任务的受控模型输入。"""
    return json.dumps(
        {
            "mail": json.loads(_mail_prompt(email)),
            "key_points": key_points or [],
            "instruction": instruction,
        },
        ensure_ascii=False,
    )


def _parse_json(raw: str) -> dict[str, Any]:
    """解析模型 JSON 输出，并拒绝 Markdown 或结构异常以外的任何猜测性修复。"""
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError as error:
        raise MailAgentError("模型未返回合法 JSON") from error
    if not isinstance(value, dict):
        raise MailAgentError("模型返回格式无效")
    return value


def _parse_extraction(raw: str) -> dict[str, Any]:
    """校验摘要工具约定的结构化字段。"""
    value = _parse_json(raw)
    points = value.get("key_points", [])
    todos = value.get("todos", [])
    due_dates = value.get("due_date_candidates", [])
    if (
        not isinstance(points, list)
        or not isinstance(due_dates, list)
        or not isinstance(todos, list)
        or not all(isinstance(item, str) for item in points + due_dates)
    ):
        raise MailAgentError("模型摘要字段格式无效")
    normalized_todos = []
    for todo in todos[:50]:
        if not isinstance(todo, dict) or not isinstance(todo.get("content"), str) or not todo["content"].strip():
            raise MailAgentError("模型待办字段格式无效")
        due_at = todo.get("due_at")
        due_text = todo.get("due_text")
        if due_at is not None and not isinstance(due_at, str):
            raise MailAgentError("模型待办截止时间格式无效")
        if due_text is not None and not isinstance(due_text, str):
            raise MailAgentError("模型待办截止时间文本格式无效")
        normalized_todos.append(
            {
                "content": todo["content"].strip(),
                "due_at": due_at,
                "due_text": due_text,
                "is_inferred": bool(todo.get("is_inferred", True)),
            }
        )
    return {
        "key_points": [item[:1000] for item in points[:20]],
        "todos": normalized_todos,
        "due_date_candidates": due_dates[:20],
    }


def _parse_reply(raw: str) -> dict[str, Any]:
    """校验并规范化邮件回复草稿。"""
    value = _parse_json(raw)
    to, cc, subject, body = value.get("to"), value.get("cc", []), value.get("subject"), value.get("body")
    if not isinstance(to, list) or not to or not all(isinstance(item, str) and "@" in item for item in to):
        raise MailAgentError("模型未返回有效收件人")
    if not isinstance(cc, list) or not all(isinstance(item, str) and "@" in item for item in cc):
        raise MailAgentError("模型未返回有效抄送人")
    if not isinstance(subject, str) or not subject.strip() or not isinstance(body, str) or not body.strip():
        raise MailAgentError("模型未返回有效邮件主题或正文")
    return {
        "to": list(dict.fromkeys(item.strip().lower() for item in to)),
        "cc": list(dict.fromkeys(item.strip().lower() for item in cc)),
        "subject": subject.strip(),
        "body": body.strip(),
    }
