"""创建用户长期记忆的请求参数。"""

import re
import json
from datetime import datetime,timezone
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator
from app.param.memory_entity import MemoryEntity


_HIGH_RISK_SECRET_PATTERN = re.compile(
    r"(?:password|密码|api[_ -]?key|密钥|private[_ -]?key|私钥|银行卡|card[_ -]?number)\s*[:：=]",
    re.IGNORECASE,
)


class MemoryCreateParam(BaseModel):
    """用户手动录入或明确确认的一条长期偏好。"""

    model_config=ConfigDict(extra='forbid')
    content: str = Field(min_length=1, max_length=4000)
    is_sensitive: bool | None = None  # 旧字段显式兼容，不允许与新分级矛盾。
    sensitivity: Literal['normal','sensitive'] | None = None
    memory_type: Literal['long_term_summary','entity'] = 'long_term_summary'
    scope: Literal['user'] = 'user'
    entity: MemoryEntity | None = None
    expires_at: AwareDatetime | None = None
    source_kind: Literal['manual','run','knowledge_document'] = 'manual'
    source_object_id: UUID | None = None
    source_version: str | None = Field(default=None,min_length=1,max_length=128)
    source_run_id: UUID | None = None  # 兼容旧confirm入口，服务端核验并固定真实版本。

    @field_validator("content")
    @classmethod
    def normalize_and_reject_secrets(cls, value: str) -> str:
        """拒绝空白与明显的高危秘密，避免把凭据当作长期记忆保存。"""
        normalized = value.strip()
        if not normalized:
            raise ValueError("记忆内容不能为空")
        if _HIGH_RISK_SECRET_PATTERN.search(normalized):
            raise ValueError("长期记忆不允许保存密码、API Key、私钥或完整银行卡信息")
        return normalized

    @model_validator(mode='after')
    def validate_memory(self):
        if self.sensitivity is not None and self.is_sensitive is not None and self.is_sensitive!=(self.sensitivity=='sensitive'):
            raise ValueError('敏感级别与is_sensitive矛盾')
        self.sensitivity=self.sensitivity or ('sensitive' if self.is_sensitive else 'normal')
        self.is_sensitive=self.sensitivity=='sensitive'
        if (self.memory_type=='entity') != (self.entity is not None): raise ValueError('实体记忆必须且仅能携带实体字段')
        combined=self.content+' '+(json.dumps(self.entity.model_dump(),ensure_ascii=False) if self.entity else '')
        if _HIGH_RISK_SECRET_PATTERN.search(combined) or re.search(r'-----BEGIN .*PRIVATE KEY-----|\bsk-[A-Za-z0-9_-]{20,}',combined):
            raise ValueError('长期记忆不允许保存高危秘密')
        if self.sensitivity=='normal' and re.search(r'身份证(?:号)?\s*[:：=]|诊断(?:结果)?\s*[:：=]|病史\s*[:：=]',combined):
            raise ValueError('该内容至少应标记为敏感；不可降级为普通记忆')
        if self.expires_at is not None and self.expires_at<=datetime.now(timezone.utc): raise ValueError('失效时间必须在未来')
        if self.source_run_id:
            if self.source_kind not in ('manual','run') or self.source_object_id not in (None,self.source_run_id):
                raise ValueError('来源任务字段矛盾')
            self.source_kind='run'; self.source_object_id=self.source_run_id
        if self.source_kind=='manual' and (self.source_object_id or self.source_version): raise ValueError('手动录入不能伪造业务来源')
        if self.source_kind!='manual' and not self.source_object_id: raise ValueError('业务来源必须提供真实对象ID')
        if self.source_kind=='knowledge_document' and not self.source_version: raise ValueError('资料来源必须提供版本ID')
        return self


class MemoryUpdateParam(MemoryCreateParam):
    # 完整表单更新需显式携带关键事实，不能因遗漏字段默默降级敏感性或清除来源/有效期。
    memory_type: Literal['long_term_summary','entity']
    sensitivity: Literal['normal','sensitive']
    source_kind: Literal['manual','run','knowledge_document']
    expires_at: AwareDatetime | None = Field(...)
    expected_version: int = Field(strict=True,ge=1)
