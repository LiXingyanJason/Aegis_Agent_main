"""知识导入仅允许业务元数据，拒绝客户端路径、模型空间和ACL字段。"""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, StrictBool


class KnowledgeImportMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    title: str = Field(min_length=1, max_length=200)
    client_request_id: str = Field(min_length=1, max_length=128)
    visibility: Literal["private", "tenant"] = "private"
    external_embedding_allowed: bool = False
    is_sensitive: bool = False


class KnowledgeTextImportParam(KnowledgeImportMetadata):
    content: str = Field(min_length=1, max_length=262144)


class KnowledgeIndexStartParam(BaseModel):
    model_config = ConfigDict(extra="forbid")
    external_embedding_allowed: StrictBool = False


class KnowledgeActivationParam(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: StrictBool


class KnowledgeVisibilityParam(BaseModel):
    model_config = ConfigDict(extra='forbid')
    visibility: Literal['private','tenant']
