"""主 Agent 决策与专家交接契约；模型不能直接指定可执行工具。"""

import json
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from aegis_agent_worker.agent.contracts.context import CardReference

Domain = Literal["calendar", "mail", "knowledge"]
Intent = Literal["list_events", "find_free_time", "arrange_meeting", "query_mail", "extract_mail", "draft_mail_reply", "query_knowledge"]


class ResourceReference(BaseModel):
    """稳定业务引用，身份与权限由服务端再次核验。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    resource_type: Literal["email", "calendar_draft"]
    id: UUID
    version: int | None = Field(default=None, strict=True, ge=1)
    source_version: str | None = Field(default=None, min_length=1, max_length=128)


class ConversationCapability(BaseModel):
    """由领域工厂显式声明的普通对话能力。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    domain: Domain
    intent: Intent
    workflow: str = Field(min_length=1, max_length=64)
    description: str = Field(max_length=300)
    requires_target: bool = False


class MainDecision(BaseModel):
    """一次受限分派、澄清或普通回复决定。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal[1] = 1
    decision: Literal["delegate", "clarify", "reply"]
    domain: Domain | None = None
    intent: Intent | None = None
    instruction: str = Field(default="", max_length=2000)
    target: ResourceReference | None = None
    clarification_question: str | None = Field(default=None, min_length=1, max_length=500)
    card_reference: CardReference | None = None

    @model_validator(mode="after")
    def validate_combination(self):
        """拒绝领域错配、缺失要求以及互斥决策字段。"""
        if self.decision == "delegate":
            if not self.domain or not self.intent or not self.instruction.strip() or self.clarification_question:
                raise ValueError("交接决定缺少领域、意图或任务要求")
            expected = ("knowledge" if self.intent == "query_knowledge" else
                        "mail" if self.intent in {"query_mail", "extract_mail", "draft_mail_reply"} else "calendar")
            if self.domain != expected:
                raise ValueError("领域与意图不匹配")
            if self.card_reference is not None and (expected != "mail" or self.intent != "query_mail"):
                raise ValueError("卡片引用仅用于邮件导航")
            if self.intent in {"extract_mail", "draft_mail_reply"} and (self.target is None or self.target.resource_type != "email"):
                raise ValueError("邮件交接必须指向一封明确邮件")
            if self.target and expected == "mail" and (not self.target.source_version or self.target.version is not None):
                raise ValueError("邮件引用必须携带快照 source_version，不使用草稿版本")
            if self.target and expected == "mail" and self.target.resource_type != "email":
                raise ValueError("邮件入口只能引用邮件")
            if expected == "calendar" and self.target is not None:
                raise ValueError("当前日历能力不支持修改已有对象")
            if expected == "knowledge" and self.target is not None:
                raise ValueError("知识查询不能使用邮件或日历对象引用")
        else:
            if self.domain or self.intent or self.target or self.instruction or self.card_reference:
                raise ValueError("澄清和普通回复不能携带执行计划")
            if (self.decision == "clarify") != bool(self.clarification_question):
                raise ValueError("澄清问题与决策不一致")
        return self

    @classmethod
    def from_text(cls, text: str):
        """解析模型 JSON，不接受任意额外字段。"""
        value = text.strip()
        if value.startswith("```") and value.endswith("```"):
            value = "\n".join(value.splitlines()[1:-1])
        return cls.model_validate(json.loads(value))


class AgentHandoff(BaseModel):
    """根图交给领域专家的已核验要求，不含正文或模型思维过程。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal[1] = 1
    domain: Domain
    intent: Intent
    instruction: str = Field(min_length=1, max_length=2000)
    target: ResourceReference | None = None


def clarify(question: str) -> MainDecision:
    """生成不含执行字段的受控澄清决定。"""
    return MainDecision(decision="clarify", clarification_question=question)
