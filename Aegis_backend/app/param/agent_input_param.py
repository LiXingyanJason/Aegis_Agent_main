"""补充回答和草稿修改请求，均不代表发送批准。"""
from pydantic import BaseModel, ConfigDict, Field, field_validator


class InputAnswerParam(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(strict=True,ge=1)
    client_response_id: str = Field(min_length=1,max_length=128)
    response: str = Field(min_length=1,max_length=2000)
    @field_validator("response","client_response_id")
    @classmethod
    def strip(cls, value):
        if not value.strip():
            raise ValueError("不能为空白")
        return value.strip()


class DraftRevisionParam(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(strict=True,ge=1)
    instruction: str = Field(min_length=1,max_length=2000)
    client_request_id: str = Field(min_length=1,max_length=128)
    @field_validator("instruction","client_request_id")
    @classmethod
    def strip(cls, value):
        if not value.strip():
            raise ValueError("不能为空白")
        return value.strip()
