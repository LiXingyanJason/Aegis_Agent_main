"""7.1记忆数据契约；不是模型授权协议，也不自动写库或注入Prompt。

身份、敏感分级、确认状态与来源版本由可信代码装配；即使通过Schema，
仍须按调用时的身份、时间和数据库来源快照复核。记忆永远不包含工具权限。
"""
from typing import Annotated, Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import re
import json
from types import MappingProxyType

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator


MemoryType = Literal['working', 'long_term_summary', 'entity']
MemoryPurpose = Literal['conversation_reply', 'mail_reply_draft', 'mail_draft_revision',
                        'calendar_planning', 'calendar_draft_personalization', 'calendar_recommendation',
                        'main_routing', 'mail_extraction', 'knowledge_answer', 'approval']
CALENDAR_MEMORY_KEYS = MappingProxyType({
    'calendar_planning':frozenset({'timezone'}),
    'calendar_draft_personalization':frozenset({'timezone','meeting_preferences'}),
    'calendar_recommendation':frozenset({'timezone','meeting_preferences'}),
})
PERSONALIZATION_PURPOSES = frozenset({'conversation_reply', 'mail_reply_draft', 'mail_draft_revision'}) | frozenset(CALENDAR_MEMORY_KEYS)
_SECRET = re.compile(r'(?:password|密码|api[_ -]?key|密钥|private[_ -]?key|私钥|银行卡|card[_ -]?number)\s*[:：=]', re.I)


class MemoryModel(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)


class TimezoneEntity(MemoryModel):
    """表示一条结构化的 “用户时区” 记忆"""
    key: Literal['timezone'] = 'timezone'
    value: str = Field(min_length=1, max_length=64)

    @field_validator('value') # 创建对象时，对 value 再做一层校验
    @classmethod
    def valid_timezone(cls, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError('必须使用有效IANA时区') from None
        return value


class ContactEntity(MemoryModel):
    """表示一条结构化的联系人记忆"""
    key: Literal['contact'] = 'contact'
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(min_length=3, max_length=254)

    @field_validator('email')
    @classmethod
    def valid_email(cls, value):
        # 保守ASCII邮箱子集；不接受显示名、换行、多个收件人或工具参数。
        if not re.fullmatch(r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]+@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+", value):
            raise ValueError('联系人邮箱格式无效')
        local = value.split('@')[0]
        if len(local) > 64 or local.startswith('.') or local.endswith('.') or '..' in local:
            raise ValueError('联系人邮箱格式无效')
        return value

    @field_validator('name')
    @classmethod
    def valid_name(cls, value):
        if not value.strip() or any(ord(char) < 32 for char in value):
            raise ValueError('联系人名称无效')
        return value.strip()


class ReplyStyleEntity(MemoryModel):
    """ 回复风格 记忆"""
    key: Literal['reply_style'] = 'reply_style'
    value: Literal['formal', 'concise', 'friendly']


class MeetingPreferencesEntity(MemoryModel):
    """仅用于可信候选排序的软偏好，不能声明空闲事实或批准写入。"""
    key: Literal['meeting_preferences'] = 'meeting_preferences'
    preferred_time_of_day: Literal['any', 'morning', 'afternoon', 'evening'] = 'any'
    avoid_weekdays: list[Annotated[int, Field(strict=True, ge=1, le=7)]] = Field(default_factory=list, max_length=7)
    strength: Literal['soft'] = 'soft'

    @field_validator('avoid_weekdays')
    @classmethod
    def unique_days(cls, value):
        if len(set(value)) != len(value):
            raise ValueError('星期不能重复')
        return sorted(value)

# 组成一个联合类型
MemoryEntity = Annotated[TimezoneEntity | ContactEntity | ReplyStyleEntity | MeetingPreferencesEntity, Field(discriminator='key')]


class MemorySourceRef(MemoryModel):
    # 不提供email_body/calendar_event/knowledge_chunk来源：原文仍在业务源中。
    kind: Literal['manual', 'conversation', 'run', 'artifact', 'knowledge_document']
    object_id: UUID
    version: str = Field(min_length=1, max_length=128, pattern=r'^\S+$')


class MemoryCandidate(MemoryModel):
    """一条待确认的记忆建议"""
    memory_id: UUID
    tenant_id: UUID
    user_id: UUID
    memory_type: MemoryType
    source: MemorySourceRef
    conversation_id: UUID | None = None
    sensitivity: Literal['normal', 'sensitive']
    created_at: AwareDatetime
    expires_at: AwareDatetime | None = None
    applicable_purposes: tuple[MemoryPurpose, ...] = ()
    content: str = Field(min_length=1, max_length=4000)
    entity: MemoryEntity | None = None
    confirmation_status: Literal['pending'] = 'pending'

    @field_validator('content')
    @classmethod
    def safe_content(cls, value):
        value = value.strip()
        if not value or _SECRET.search(value):
            raise ValueError('记忆为空或包含明显高危秘密')
        return value

    @model_validator(mode='after')
    def valid_shape(self):
        if (self.memory_type == 'entity') != (self.entity is not None):
            raise ValueError('实体类型必须且仅能携带结构化实体字段')
        if self.memory_type == 'working' and self.conversation_id is None:
            raise ValueError('工作记忆必须限定会话')
        if self.memory_type != 'working' and self.conversation_id is not None:
            raise ValueError('长期记忆不绑定单一会话')
        if self.expires_at is not None and self.expires_at <= self.created_at:
            raise ValueError('失效时间必须晚于创建时间')
        if len(set(self.applicable_purposes)) != len(self.applicable_purposes):
            raise ValueError('适用用途不能重复')
        return self


class MemoryRecord(MemoryCandidate):
    """持久事实的读取契约；不是现有memory_items表的直接ORM映射。"""
    version: int = Field(strict=True, ge=1)
    confirmation_status: Literal['pending', 'confirmed', 'revoked', 'superseded', 'deleted']
    confirmed_at: AwareDatetime | None = None
    updated_at: AwareDatetime

    @model_validator(mode='after')
    def valid_confirmation(self):
        if self.updated_at < self.created_at:
            raise ValueError('更新时间不能早于创建时间')
        if self.confirmation_status == 'confirmed' and self.confirmed_at is None:
            raise ValueError('已确认记忆必须有确认时间')
        if self.confirmation_status == 'pending' and self.confirmed_at is not None:
            raise ValueError('待确认候选不能携带确认时间')
        if self.confirmed_at is not None and not self.created_at <= self.confirmed_at <= self.updated_at:
            raise ValueError('确认时间无效')
        return self


class MemoryContextItem(MemoryModel):
    """经过校验的有界摘录，保留来源与版本，不是完整历史或系统消息。"""
    memory_id: UUID
    tenant_id: UUID
    user_id: UUID
    version: int = Field(strict=True, ge=1)
    memory_type: MemoryType
    source: MemorySourceRef
    excerpt: str = Field(min_length=1, max_length=4000)
    selection_reason: Literal['relevant_confirmed_preference', 'confirmed_summary', 'session_fact']

    @field_validator('excerpt')
    @classmethod
    def safe_excerpt(cls, value):
        return MemoryCandidate.safe_content(value)


class MemoryContextBundle(MemoryModel):
    """为一次任务、某一种模型用途选出的记忆集合"""
    tenant_id: UUID
    user_id: UUID
    run_id: UUID
    conversation_id: UUID | None
    purpose: MemoryPurpose
    policy_version: str = Field(min_length=1, max_length=64)
    selected_at: AwareDatetime
    items: tuple[MemoryContextItem, ...] = Field(default=(), max_length=50)
    # Token未知不填0；字符/UTF-8字节不是计费Token。
    estimated_tokens: int | None = Field(default=None, strict=True, ge=0)

    @model_validator(mode='after')
    def valid_items(self):
        if self.items and self.purpose not in PERSONALIZATION_PURPOSES:
            raise ValueError('该用途不允许个性化记忆')
        if len({item.memory_id for item in self.items}) != len(self.items):
            raise ValueError('同一记忆不能重复注入')
        if any((item.tenant_id, item.user_id) != (self.tenant_id, self.user_id) for item in self.items):
            raise ValueError('记忆身份与上下文不一致')
        if self.purpose in CALENDAR_MEMORY_KEYS:
            seen_keys=set()
            for item in self.items:
                if item.memory_type!='entity':
                    raise ValueError('日历用途不接受自由文本摘要')
                entity=TypeAdapter(MemoryEntity).validate_python(json.loads(item.excerpt))
                if entity.key not in CALENDAR_MEMORY_KEYS[self.purpose]:
                    raise ValueError('日历用途不接受该实体')
                if entity.key in seen_keys:
                    raise ValueError('日历实体偏好不能存在多个生效值')
                seen_keys.add(entity.key)
        return self

    @property
    def content_bytes(self):
        return sum(len(item.excerpt.encode('utf-8')) for item in self.items)

    @property
    def content_chars(self):
        return sum(len(item.excerpt) for item in self.items)


class MemoryUsage(MemoryModel):
    """单次调用的安全审计契约，不携带记忆正文、完整Prompt或凭据。"""
    tenant_id: UUID
    user_id: UUID
    run_id: UUID
    attempt_id: UUID
    purpose: MemoryPurpose
    memory_id: UUID
    memory_version: int = Field(strict=True, ge=1)
    memory_type: MemoryType
    source: MemorySourceRef
    used_at: AwareDatetime
    included_in_prompt: Literal[True] = True
    selection_reason: Literal['relevant_confirmed_preference', 'confirmed_summary', 'session_fact']
    policy_version: str = Field(min_length=1, max_length=64)
    excerpt_bytes: int = Field(strict=True, ge=1)
    estimated_tokens: int | None = Field(default=None, strict=True, ge=0)
    # 供应商usage属于整次调用，不能伪装成该条记忆的精确Token消耗。
    call_prompt_tokens: int | None = Field(default=None, strict=True, ge=0)

    @model_validator(mode='after')
    def allowed_purpose(self):
        if self.purpose not in PERSONALIZATION_PURPOSES:
            raise ValueError('该用途不允许记忆使用记录')
        return self
