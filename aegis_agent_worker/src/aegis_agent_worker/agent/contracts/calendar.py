"""日历领域的受控计划、候选时段与展示 Artifact 构造规则。"""

import hashlib
import json
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from aegis_agent_worker.agent.contracts.common import (
    ConversationArtifact,
    ToolResultSummary,
    ensure_json_object_size,
)
from aegis_agent_worker.tool.contracts import ToolInvocation

MAX_CALENDAR_ARTIFACT_ITEMS = 20


class CalendarDecision(BaseModel):
    """日历专家的受限领域计划；时间事实仍由 MCP 查询取得。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal[1] = 1
    action: Literal["list_events", "find_free_time", "prepare_meeting", "clarify"]
    time_mode: Literal["explicit_interval", "search_window"] | None = None
    timezone: str = "Asia/Shanghai"
    start_at: datetime | None = None
    end_at: datetime | None = None
    duration_minutes: int | None = Field(default=None, strict=True, ge=15, le=480)
    title: str | None = Field(default=None, min_length=1, max_length=200)
    attendees: list[str] = Field(default_factory=list, max_length=10)
    preferences: str = Field(default="", max_length=1000)
    clarification_question: str | None = Field(default=None, min_length=1, max_length=500)

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError("无效的 IANA 时区") from error
        return value

    @field_validator("attendees")
    @classmethod
    def valid_attendees(cls, values):
        normalized = [value.strip().lower() for value in values]
        if len(set(normalized)) != len(normalized) or not all(_is_email(value) for value in normalized):
            raise ValueError("参会人邮箱无效或重复")
        return normalized

    @model_validator(mode="after")
    def validate_action(self):
        if self.action == "clarify":
            if (not self.clarification_question or self.start_at is not None or self.end_at is not None
                    or self.time_mode is not None or self.duration_minutes is not None
                    or self.title is not None or self.attendees or self.preferences):
                raise ValueError("澄清决定不能携带执行参数")
            return self
        if self.clarification_question or self.start_at is None or self.end_at is None or self.time_mode is None:
            raise ValueError("日历计划缺少有效时间范围")
        zone = ZoneInfo(self.timezone)
        start = self.start_at.replace(tzinfo=zone) if self.start_at.tzinfo is None else self.start_at.astimezone(zone)
        end = self.end_at.replace(tzinfo=zone) if self.end_at.tzinfo is None else self.end_at.astimezone(zone)
        if not timedelta(0) < end - start <= timedelta(days=62):
            raise ValueError("查询范围必须大于零且不超过62天")
        object.__setattr__(self, "start_at", start)
        object.__setattr__(self, "end_at", end)
        if self.action != "list_events":
            if self.duration_minutes is None:
                raise ValueError("空闲查询和会议安排必须给出时长")
            duration = timedelta(minutes=self.duration_minutes)
            if end - start < duration:
                raise ValueError("查询时段不足以容纳会议")
            if self.time_mode == "explicit_interval" and end - start != duration:
                raise ValueError("指定时段与会议时长不一致")
        if self.action == "prepare_meeting":
            if self.title is None:
                object.__setattr__(self, "title", "待确认会议")
            elif not self.title.strip():
                raise ValueError("会议主题不能是空白文本")
        return self

    @classmethod
    def from_text(cls, value):
        return cls.model_validate(json.loads(_strip_json_fence(value)))

    def to_read_plan(self):
        """由代码选择白名单工具，指定时段使用日程查询做冲突检查。"""
        if self.action == "clarify":
            raise ValueError("澄清决定不能执行工具")
        action = "list_events" if self.action == "list_events" or self.time_mode == "explicit_interval" else "find_free_time"
        arguments = {
            "start_at": self.start_at.isoformat(), "end_at": self.end_at.isoformat(),
            "timezone": self.timezone, "requested_date": self.start_at.date().isoformat(),
        }
        if action == "find_free_time":
            arguments.update(duration_minutes=self.duration_minutes, participants=self.attendees)
        return CalendarReadPlan(action=action, tool_name=f"calendar.{action}", arguments=arguments)


class CalendarReadPlan(BaseModel):
    """日历读取工作流接受的计划，而非单个工具的一对一包装。

    新增与时间范围读取语义相同的日历工具时，可扩展本计划的 action、
    tool_name 及对应参数校验；写操作或语义不同的查询应创建独立领域计划。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1] = 1
    domain: Literal["calendar"] = "calendar"
    action: Literal["list_events", "find_free_time"]
    tool_name: Literal["calendar.list_events", "calendar.find_free_time"]
    arguments: dict[str, Any]
    risk_level: Literal["read"] = "read"

    @model_validator(mode="after")
    def ensure_tool_matches_read_action(self) -> "CalendarReadPlan":
        """阻止计划动作与只读白名单工具名称不一致。"""
        expected_tool = f"calendar.{self.action}"
        if self.tool_name != expected_tool:
            raise ValueError("日历计划的 action 与 tool_name 必须一致")
        required = {"start_at", "end_at"}
        if not required.issubset(self.arguments):
            raise ValueError("日历读取计划缺少 start_at 或 end_at")
        if not all(isinstance(self.arguments[key], str) and self.arguments[key] for key in required):
            raise ValueError("日历读取计划的 start_at 与 end_at 必须是非空字符串")
        if self.action == "find_free_time":
            self._validate_free_time_arguments()
        return self

    def _validate_free_time_arguments(self) -> None:
        """校验空闲时间工具特有的受控参数。"""
        duration = self.arguments.get("duration_minutes", 30)
        if not isinstance(duration, int) or isinstance(duration, bool) or not 15 <= duration <= 480:
            raise ValueError("空闲时间查询的 duration_minutes 必须在 15 到 480 之间")
        participants = self.arguments.get("participants", [])
        if not isinstance(participants, list) or len(participants) > 10:
            raise ValueError("空闲时间查询的 participants 必须是不超过 10 项的列表")
        if not all(isinstance(participant, str) and participant for participant in participants):
            raise ValueError("空闲时间查询的 participants 必须仅包含非空字符串")

    @field_validator("arguments")
    @classmethod
    def validate_arguments(cls, value: dict[str, Any]) -> dict[str, Any]:
        """确保 Checkpoint 只接收小型 JSON 参数而不是任意对象。"""
        ensure_json_object_size(value, field_name="日历计划参数")
        return value

    @classmethod
    def from_invocation(cls, invocation: ToolInvocation) -> "CalendarReadPlan":
        """将日历专家生成的调用转为可检查点持久化的工作流计划。"""
        action_by_tool = {
            "calendar.list_events": "list_events",
            "calendar.find_free_time": "find_free_time",
        }
        try:
            action = action_by_tool[invocation.tool_name]
        except KeyError as error:
            raise ValueError("日历读取计划包含未允许的工具") from error
        return cls(action=action, tool_name=invocation.tool_name, arguments=invocation.arguments)

    def to_invocation(self) -> ToolInvocation:
        """仅在 Schema 校验通过后重建实际工具调用。"""
        return ToolInvocation(tool_name=self.tool_name, arguments=self.arguments)


class CalendarFreeTimeCandidate(BaseModel):
    """由 Calendar MCP 计算出的、可被推荐但不可由模型伪造的候选时段。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    slot_id: str = Field(min_length=8, max_length=64, pattern=r"^slot_[a-f0-9]+$")
    start_at: str = Field(min_length=1, max_length=64)
    end_at: str = Field(min_length=1, max_length=64)
    start_at_local: str | None = Field(default=None, max_length=64)
    end_at_local: str | None = Field(default=None, max_length=64)


class CalendarSlotRecommendation(BaseModel):
    """模型对可信候选时段的排序结果，不携带可执行的外部写入参数。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1] = 1
    selected_slot_id: str = Field(min_length=8, max_length=64, pattern=r"^slot_[a-f0-9]+$")
    alternative_slot_ids: list[str] = Field(default_factory=list, max_length=5)
    reason: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def ensure_no_duplicate_slots(self) -> "CalendarSlotRecommendation":
        """推荐时段不能同时作为备选，备选之间也不能重复。"""
        if self.selected_slot_id in self.alternative_slot_ids:
            raise ValueError("推荐时段不能同时出现在备选时段中")
        if len(set(self.alternative_slot_ids)) != len(self.alternative_slot_ids):
            raise ValueError("备选时段不能重复")
        return self

    @classmethod
    def from_llm_text(
        cls, content: str, candidates: list[CalendarFreeTimeCandidate]
    ) -> "CalendarSlotRecommendation":
        """解析模型 JSON，并确保每一个选择均来自 MCP 返回的候选集合。"""
        try:
            payload = json.loads(_strip_json_fence(content))
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise ValueError("模型未返回有效的候选时段推荐 JSON") from error
        recommendation = cls.model_validate(payload)
        candidate_ids = {candidate.slot_id for candidate in candidates}
        selected_ids = {recommendation.selected_slot_id, *recommendation.alternative_slot_ids}
        if not selected_ids.issubset(candidate_ids):
            raise ValueError("模型推荐了 MCP 未返回的时间段")
        return recommendation


class CalendarMeetingDraftProposal(BaseModel):
    """LLM 生成的站内会议草稿；只能引用 MCP 已返回的候选时段。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1] = 1
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=4000)
    attendees: list[str] = Field(default_factory=list, max_length=10)
    selected_slot_id: str = Field(min_length=8, max_length=64, pattern=r"^slot_[a-f0-9]+$")
    alternative_slot_ids: list[str] = Field(default_factory=list, max_length=5)
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str) -> str:
        """标题仅允许模型输出可展示的简短文本。"""
        normalized = value.strip()
        if not normalized:
            raise ValueError("会议主题不能为空")
        return normalized

    @field_validator("description")
    @classmethod
    def normalize_description(cls, value: str) -> str:
        """描述允许为空，但不保留无意义首尾空白。"""
        return value.strip()

    @field_validator("attendees")
    @classmethod
    def validate_attendees(cls, value: list[str]) -> list[str]:
        """一期仅允许模型保留或补充格式正确的邮箱参会人。"""
        normalized = [email.strip().lower() for email in value]
        if len(set(normalized)) != len(normalized):
            raise ValueError("参会人邮箱不能重复")
        if not all(_is_email(value) for value in normalized):
            raise ValueError("参会人必须是有效邮箱地址")
        return normalized

    @model_validator(mode="after")
    def ensure_candidate_selection_is_valid(self) -> "CalendarMeetingDraftProposal":
        """一个推荐时段不能同时作为备选，备选之间不能重复。"""
        if self.selected_slot_id in self.alternative_slot_ids:
            raise ValueError("推荐时段不能同时出现在备选时段中")
        if len(set(self.alternative_slot_ids)) != len(self.alternative_slot_ids):
            raise ValueError("备选时段不能重复")
        return self

    @classmethod
    def from_llm_text(
        cls, content: str, candidates: list[CalendarFreeTimeCandidate]
    ) -> "CalendarMeetingDraftProposal":
        """解析 LLM 草稿 JSON，并限制时间选择只能来自 MCP 候选集合。"""
        try:
            payload = json.loads(_strip_json_fence(content))
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise ValueError("模型未返回有效的会议草稿 JSON") from error
        # 备选数量是展示限制：先验证完整列表，再按模型顺序保留前五项。
        # 不能先截断，否则尾部伪造的候选或重复项会被悄悄隐藏。
        if isinstance(payload, dict):
            alternatives = payload.get("alternative_slot_ids", [])
            candidate_ids = {candidate.slot_id for candidate in candidates}
            if not isinstance(alternatives, list) or not all(
                isinstance(slot_id, str) and slot_id in candidate_ids
                for slot_id in alternatives
            ):
                raise ValueError("模型草稿的备选时间段无效")
            if len(set(alternatives)) != len(alternatives):
                raise ValueError("备选时段不能重复")
            if payload.get("selected_slot_id") in alternatives:
                raise ValueError("推荐时段不能同时出现在备选时段中")
            payload["alternative_slot_ids"] = alternatives[:5]
        proposal = cls.model_validate(payload)
        candidate_ids = {candidate.slot_id for candidate in candidates}
        selected_ids = {proposal.selected_slot_id, *proposal.alternative_slot_ids}
        if not selected_ids.issubset(candidate_ids):
            raise ValueError("模型草稿引用了 MCP 未返回的时间段")
        return proposal


def extract_calendar_free_time_candidates(
    summary: dict[str, Any] | None,
) -> tuple[list[CalendarFreeTimeCandidate], int | None]:
    """将工具摘要转换为带稳定 ID 的候选时段，供模型排序和代码校验使用。"""
    if not isinstance(summary, dict):
        return [], None
    raw_slots = summary.get("available_slots")
    if not isinstance(raw_slots, list):
        return [], None
    candidates: list[CalendarFreeTimeCandidate] = []
    for item in raw_slots[:10]:
        if not isinstance(item, dict):
            continue
        start_at = item.get("start_at")
        end_at = item.get("end_at")
        if not isinstance(start_at, str) or not start_at or not isinstance(end_at, str) or not end_at:
            continue
        slot_id = "slot_" + hashlib.sha256(f"{start_at}|{end_at}".encode("utf-8")).hexdigest()[:16]
        candidates.append(
            CalendarFreeTimeCandidate(
                slot_id=slot_id,
                start_at=start_at,
                end_at=end_at,
                start_at_local=item.get("start_at_local") if isinstance(item.get("start_at_local"), str) else None,
                end_at_local=item.get("end_at_local") if isinstance(item.get("end_at_local"), str) else None,
            )
        )
    duration = summary.get("duration_minutes")
    return candidates, duration if isinstance(duration, int) and not isinstance(duration, bool) else None


def build_calendar_read_artifact(
    plan: CalendarReadPlan, result: ToolResultSummary
) -> ConversationArtifact | None:
    """从可信日历读取摘要中提取页面卡片需要的最小字段。"""
    if result.status != "succeeded":
        return None
    summary = result.summary
    timezone = summary.get("display_timezone")
    if plan.action == "list_events":
        events = summary.get("events")
        if not isinstance(events, list):
            return None
        items = [
            _pick_event_fields(item)
            for item in events[:MAX_CALENDAR_ARTIFACT_ITEMS]
            if isinstance(item, dict)
        ]
        return ConversationArtifact(
            artifact_type="calendar_events",
            payload={"display_timezone": timezone, "items": items},
        )

    slots = summary.get("available_slots")
    if not isinstance(slots, list):
        return None
    items = [
        _pick_slot_fields(item)
        for item in slots[:MAX_CALENDAR_ARTIFACT_ITEMS]
        if isinstance(item, dict)
    ]
    return ConversationArtifact(
        artifact_type="calendar_free_time",
        payload={
            "display_timezone": timezone,
            "duration_minutes": summary.get("duration_minutes"),
            "items": items,
        },
    )


def _pick_event_fields(value: dict[str, Any]) -> dict[str, Any]:
    """移除描述、参会人等非卡片必需字段，避免扩大对话展示数据。"""
    return {
        key: value[key]
        for key in (
            "event_id",
            "external_event_id",
            "title",
            "start_at",
            "end_at",
            "start_at_local",
            "end_at_local",
        )
        if isinstance(value.get(key), str)
    }


def _pick_slot_fields(value: dict[str, Any]) -> dict[str, Any]:
    """提取空闲时段的时间字段。"""
    return {
        key: value[key]
        for key in ("start_at", "end_at", "start_at_local", "end_at_local")
        if isinstance(value.get(key), str)
    }


def _strip_json_fence(value: str) -> str:
    """兼容模型偶尔包裹的 Markdown JSON 代码块。"""
    stripped = value.strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 3:
            return "\n".join(lines[1:-1]).strip()
    return stripped


def _is_email(value: str) -> bool:
    """以最小且可预测的规则校验一期会议参会人邮箱。"""
    return bool(re.fullmatch(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,63}", value))

