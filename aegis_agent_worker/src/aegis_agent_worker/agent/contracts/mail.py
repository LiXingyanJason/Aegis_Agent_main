"""邮件查询计划、元数据与候选卡片契约，不包含正文。"""

import json
from datetime import timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, AwareDatetime, model_validator

from aegis_agent_worker.agent.contracts.common import ConversationArtifact
from aegis_agent_worker.agent.contracts.decision import ResourceReference


class MailQueryPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal[1] = 1
    action: Literal["list", "filter", "clarify"]
    keywords: list[str] = Field(default_factory=list, max_length=5)
    sender_contains: str | None = Field(default=None, min_length=1, max_length=100)
    subject_equals: str | None = Field(default=None, min_length=1, max_length=998)
    start_at: AwareDatetime | None = None
    end_at: AwareDatetime | None = None
    limit: int = Field(default=10, strict=True, ge=1, le=20)
    clarification_question: str | None = Field(default=None, min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_query(self):
        if any(not keyword.strip() or len(keyword) > 100 for keyword in self.keywords):
            raise ValueError("关键词必须是非空短文本")
        if (self.start_at is None) != (self.end_at is None):
            raise ValueError("查询时间必须同时给出起止")
        if self.start_at and not timedelta(0) < self.end_at - self.start_at <= timedelta(days=62):
            raise ValueError("查询范围须大于零且不超过62天")
        filters = bool(self.keywords or self.sender_contains or self.subject_equals or self.start_at)
        if self.action == "clarify":
            if not self.clarification_question or filters:
                raise ValueError("澄清不能携带筛选条件")
        elif self.clarification_question or (self.action == "list" and filters):
            raise ValueError("查询动作与筛选条件不一致")
        elif self.action == "filter" and not filters:
            raise ValueError("筛选动作必须有明确条件")
        return self

    def matches(self, message):
        """对元数据执行确定性 AND 筛选，不让模型挑选或虚构结果。"""
        if self.action == "clarify":
            return False
        if self.subject_equals and self.subject_equals.strip().casefold() != message.subject.strip().casefold():
            return False
        if self.start_at and not self.start_at <= message.received_at < self.end_at:
            return False
        sender = f"{message.sender_name or ''} {message.sender_email}".casefold()
        if self.sender_contains and self.sender_contains.casefold() not in sender:
            return False
        text = f"{message.subject} {sender} {message.list_preview or ''}".casefold()
        return all(keyword.strip().casefold() in text for keyword in self.keywords)


class MailDecision(BaseModel):
    """邮件领域统一决定；每次只允许一个查询、提取或起草动作。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal[1] = 1
    action: Literal["query", "extract", "draft_reply", "clarify"]
    query: MailQueryPlan | None = None
    target: ResourceReference | None = None
    instruction: str = Field(default="", max_length=2000)
    clarification_question: str | None = Field(default=None, min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_action(self):
        if self.action == "clarify":
            if not self.clarification_question or self.query or self.target or self.instruction:
                raise ValueError("澄清决定不能携带执行参数")
        elif self.clarification_question:
            raise ValueError("执行决定不能包含澄清问题")
        elif self.action == "query":
            if self.query is None or self.query.action == "clarify":
                raise ValueError("查询决定必须包含查询计划")
            if self.target and (self.target.resource_type != "email" or not self.target.source_version
                                or self.target.version is not None):
                raise ValueError("定位卡片必须使用有效邮件引用")
        elif (self.query is not None or self.target is None or self.target.resource_type != "email"
              or not self.target.source_version or self.target.version is not None):
            raise ValueError("摘要或起草必须指向明确邮件及来源版本")
        return self


class MailListMessage(BaseModel):
    """与主后端邮件列表同步契约保持一致，丢弃额外字段及完整正文。"""
    provider_message_id: str = Field(min_length=1, max_length=512)
    provider_thread_id: str | None = Field(default=None, max_length=512)
    sender_name: str | None = Field(default=None, max_length=256)
    sender_email: str = Field(min_length=1, max_length=320)
    subject: str = Field(min_length=1, max_length=998)
    received_at: AwareDatetime
    source_version: str = Field(min_length=1, max_length=128)
    list_preview: str | None = Field(default=None, max_length=1000)
    attachment_count: int = Field(ge=0)


def build_mail_candidates(run, plan, rows, matched_count, source, focused=False):
    """卡片保持稳定顺序，数量或字节裁剪必须显式标记，不静默丢失。"""
    items = [{
        "position": index + 1, "email_id": str(row["id"]), "source_version": row["source_version"],
        "subject": row["subject"][:160], "sender_name": (row["sender_name"] or "")[:64],
        "sender_email": row["sender_email"], "received_at": row["received_at"].isoformat(),
        "extraction_status": row["extraction_status"],
    } for index, row in enumerate(rows[:plan.limit])]
    payload = dict(query_id=str(run.id), source=source, matched_count=matched_count,
                   returned_count=len(items), truncated=matched_count > len(items), items=items)
    payload["display_mode"] = "single" if focused and matched_count == 1 else "list"
    while len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > 10_000 and items:
        items.pop()
        payload.update(returned_count=len(items), truncated=True)
    return ConversationArtifact(artifact_type="mail_candidates", payload=payload)
