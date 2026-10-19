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


MemoryEntity=Annotated[TimezoneEntity|ContactEntity|ReplyStyleEntity,Field(discriminator='key')]
