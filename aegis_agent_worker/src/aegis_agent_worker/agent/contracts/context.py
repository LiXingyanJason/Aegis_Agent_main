"""主决策仅可请求站内卡片读取；不暴露身份、SQL或外部业务工具。"""
from typing import Literal, Any
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, model_validator, AwareDatetime


class CardsListArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    card_type: Literal["mail_candidates"] = "mail_candidates"
    limit: int = Field(default=3, strict=True, ge=1, le=5)


class CardsGetArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    card_type: Literal["mail_candidates"] = "mail_candidates"
    card_id: UUID | None = None
    selector: Literal["latest"] | None = None
    @model_validator(mode="after")
    def exactly_one(self):
        if (self.card_id is None) == (self.selector is None):
            raise ValueError("必须且只能指定card_id或selector")
        return self


CONTEXT_SCHEMAS = {"conversation.cards.list": CardsListArgs, "conversation.cards.get": CardsGetArgs}


class MainContextReadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    action: Literal["read_context"]
    tool_name: Literal["conversation.cards.list", "conversation.cards.get"]
    arguments: dict[str, Any] = Field(default_factory=dict)
    @model_validator(mode="after")
    def valid_arguments(self):
        arguments = CONTEXT_SCHEMAS[self.tool_name].model_validate(self.arguments)
        object.__setattr__(self, "arguments", arguments.model_dump(mode="json", exclude_none=True))
        return self


class CardReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    card_id: UUID
    query_id: UUID
    position: int = Field(strict=True, ge=1, le=20)


class CardHeader(BaseModel):
    model_config = ConfigDict(extra="forbid")
    card_id: UUID
    query_id: UUID
    card_type: Literal["mail_candidates"]
    status: Literal["valid", "expired"]
    created_at: AwareDatetime
    expires_at: AwareDatetime


class CardIndex(CardHeader):
    item_count: int = Field(ge=0)


class CardItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    position: int = Field(strict=True, ge=1, le=20)
    status: Literal["valid", "stale"]
    email_id: UUID | None = None
    source_version: str | None = Field(default=None, min_length=1, max_length=128)
    subject: str | None = Field(default=None, max_length=200)
    @model_validator(mode="after")
    def valid_reference(self):
        if self.status == "valid" and (self.email_id is None or self.source_version is None):
            raise ValueError("有效条目缺少对象引用")
        if self.status == "stale" and (self.email_id is not None or self.source_version is not None):
            raise ValueError("失效条目不得提供对象引用")
        return self


class CardSnapshot(CardHeader):
    items: list[CardItem] = Field(default_factory=list, max_length=20)


class CardListResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cards: list[CardIndex] = Field(default_factory=list, max_length=5)
