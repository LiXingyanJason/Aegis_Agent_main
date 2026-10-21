"""可公开的输出校验诊断；不能携带输入值、异常文本或任意字段名。"""
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator


SAFE_FIELDS = frozenset({"facts", "title", "timezone", "time_windows", "start_at", "end_at",
    "duration_minutes", "attendees", "evidence_quotes", "action", "query", "reference", "question",
    "resource_type", "id", "version", "source_version", "keywords", "subject_equals", "limit", "field",
    "window_index", "proposed_requirements", "selected_slot_id", "alternative_slot_ids", "description", "reason"})


class OutputValidationIssue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    path: str = ""
    type: Literal["missing", "extra_forbidden", "literal_error", "int_type", "int_parsing",
        "string_type", "string_too_long", "string_too_short", "list_type", "tuple_type",
        "model_type", "dict_type", "datetime_parsing", "datetime_type", "timezone_aware",
        "greater_than_equal", "less_than_equal", "too_long", "too_short", "value_error",
        "schema_invalid", "invalid_json", "invalid_output", "evidence_quote_mismatch",
        "evidence_required", "body_as_title", "invalid_attendee_email", "duplicate_attendee_email",
        "action_parameter_mismatch"]

    @field_validator("path")
    @classmethod
    def safe_path(cls, value):
        if len(value) > 160 or (value and any(part not in SAFE_FIELDS for part in value.split("."))):
            raise ValueError("诊断只能包含安全字段路径")
        return value
