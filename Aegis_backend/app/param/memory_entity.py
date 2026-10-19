"""用户设置的结构实体，与Worker契约同义但无跨进程包依赖。"""
import re
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from pydantic import BaseModel, ConfigDict, Field, field_validator


class EntityModel(BaseModel):
    model_config=ConfigDict(extra='forbid',frozen=True)


class TimezoneEntity(EntityModel):
    key: Literal['timezone']
    value: str = Field(min_length=1,max_length=64)

    @field_validator('value')
    @classmethod
    def timezone(cls,value):
        try: ZoneInfo(value)
        except (ZoneInfoNotFoundError,ValueError): raise ValueError('必须是有效IANA时区') from None
        return value


class ContactEntity(EntityModel):
    key: Literal['contact']
    name: str = Field(min_length=1,max_length=100)
    email: str = Field(min_length=3,max_length=254)

    @field_validator('email')
    @classmethod
    def email_address(cls,value):
        if not re.fullmatch(r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]+@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+",value):
            raise ValueError('联系人邮箱格式无效')
        local=value.split('@')[0]
        if len(local)>64 or local.startswith('.') or local.endswith('.') or '..' in local:
            raise ValueError('联系人邮箱格式无效')
        return value

    @field_validator('name')
    @classmethod
    def name_text(cls,value):
        if not value.strip() or any(ord(c)<32 for c in value): raise ValueError('联系人名称无效')
        return value.strip()


class ReplyStyleEntity(EntityModel):
    key: Literal['reply_style']
    value: Literal['formal','concise','friendly']


class MeetingPreferencesEntity(EntityModel):
    key: Literal['meeting_preferences']
    preferred_time_of_day: Literal['any','morning','afternoon','evening'] = 'any'
    avoid_weekdays: list[Annotated[int,Field(strict=True,ge=1,le=7)]] = Field(default_factory=list,max_length=7)
    strength: Literal['soft'] = 'soft'

    @field_validator('avoid_weekdays')
    @classmethod
    def unique_days(cls,value):
        if len(set(value))!=len(value): raise ValueError('星期不能重复')
        return sorted(value)


MemoryEntity=Annotated[TimezoneEntity|ContactEntity|ReplyStyleEntity|MeetingPreferencesEntity,Field(discriminator='key')]


def describe_memory_entity(entity):
    """仅由已校验的结构字段生成说明；不是模型生成，也不授予业务权限。"""
    if entity.key=='timezone':
        return f'默认时区：{entity.value}'
    if entity.key=='reply_style':
        return '邮件语气：'+{'formal':'正式','concise':'简洁','friendly':'友好'}[entity.value]
    if entity.key=='contact':
        # 不把联系人地址自动复制到列表可展示的正文预览中。
        return '常用联系人设置'
    period={'any':'无偏好','morning':'上午','afternoon':'下午','evening':'晚上'}[entity.preferred_time_of_day]
    weekdays=['周一','周二','周三','周四','周五','周六','周日']
    avoided='、'.join(weekdays[day-1] for day in entity.avoid_weekdays) or '无'
    return f'会议软偏好：时段{period}；尽量避开{avoided}（仅作软偏好）'
