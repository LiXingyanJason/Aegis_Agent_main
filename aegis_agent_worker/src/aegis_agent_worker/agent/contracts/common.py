"""跨领域复用的计划执行结果与对话展示契约。"""

import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_STRUCTURED_JSON_BYTES = 12_000


class ToolResultSummary(BaseModel):
    """供后续图节点使用的脱敏、大小受限工具执行摘要。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1] = 1
    status: Literal["succeeded", "failed"]
    tool_name: str = Field(min_length=1, max_length=128)
    summary: dict[str, Any]

    @field_validator("summary")
    @classmethod
    def validate_summary(cls, value: dict[str, Any]) -> dict[str, Any]:
        """拒绝把大载荷或不可 JSON 序列化的外部响应放进 State。"""
        ensure_json_object_size(value, field_name="工具结果摘要")
        return value


class ConversationArtifact(BaseModel):
    """供前端展示、短期指代与后续任务复用的安全业务引用。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1] = 1
    # Artifact 类型由各领域构造器定义；统一格式避免任意文本进入持久化索引。
    artifact_type: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    payload: dict[str, Any]

    @field_validator("payload")
    @classmethod
    def validate_payload(cls, value: dict[str, Any]) -> dict[str, Any]:
        """Artifact 只能保存小型、可 JSON 序列化的展示数据。"""
        ensure_json_object_size(value, field_name="对话展示引用")
        return value


def ensure_json_object_size(value: dict[str, Any], *, field_name: str) -> None:
    """统一检查结构化 State 的 JSON 可序列化性与大小上限。"""
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field_name}必须是 JSON 对象") from error
    if len(encoded.encode("utf-8")) > MAX_STRUCTURED_JSON_BYTES:
        raise ValueError(f"{field_name}超过 {MAX_STRUCTURED_JSON_BYTES} 字节限制")
