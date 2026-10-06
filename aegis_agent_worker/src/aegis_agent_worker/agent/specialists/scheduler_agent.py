"""日程专职 Agent：以确定性规则选择一期只读日历工具。"""

import re
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aegis_agent_worker.tool.contracts import ToolInvocation


class SchedulerAgent:
    """根据最新用户消息选择受限的只读日历工具，不允许模型任意调用工具。"""

    _free_time_keywords = ("空闲", "可用时间", "方便", "找时间", "约", "会议", "安排")
    _calendar_keywords = ("日程", "日历", "行程", "安排", "会议", "空闲", "可用时间")

    def __init__(self, timezone_name: str = "Asia/Shanghai") -> None:
        """以用户所在时区解释“今天”“明天”等相对日期。"""
        try:
            self._timezone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            # 配置不正确时仍使用中国项目本地开发的默认时区，避免相对日期悄悄退回 UTC。
            self._timezone = ZoneInfo("Asia/Shanghai")

    def select_read_tool(self, user_content: str) -> ToolInvocation | None:
        """为明确日历意图返回结构化调用；无匹配时返回 None。"""
        normalized = user_content.strip()
        if not normalized or not any(keyword in normalized for keyword in self._calendar_keywords):
            return None
        start_at, end_at, requested_date = _resolve_time_range(normalized, self._timezone)
        time_arguments = {
            "start_at": start_at,
            "end_at": end_at,
            # MCP 服务会忽略展示元数据；它用于工具预览和给 LLM 的可信上下文。
            "requested_date": requested_date,
            "timezone": self._timezone.key,
        }
        if any(keyword in normalized for keyword in self._free_time_keywords):
            return ToolInvocation(
                tool_name="calendar.find_free_time",
                arguments={
                    **time_arguments,
                    "duration_minutes": _resolve_duration(normalized),
                    "participants": re.findall(r"[\w.+-]+@[\w.-]+", normalized)[:10],
                },
            )
        return ToolInvocation(
            tool_name="calendar.list_events",
            arguments=time_arguments,
        )

    def select_meeting_availability_tool(self, user_content: str) -> ToolInvocation:
        """为会议安排固定选择空闲时间查询，禁止在确认前调用写工具。"""
        start_at, end_at, requested_date = _resolve_time_range(user_content, self._timezone)
        return ToolInvocation(
            tool_name="calendar.find_free_time",
            arguments={
                "start_at": start_at,
                "end_at": end_at,
                "requested_date": requested_date,
                "timezone": self._timezone.key,
                "duration_minutes": _resolve_duration(user_content),
                "participants": re.findall(r"[\w.+-]+@[\w.-]+", user_content)[:10],
            },
        )

    @staticmethod
    def meeting_title(user_content: str) -> str:
        """一期使用安全默认标题，后续由受控 LLM/表单补充会议主题。"""
        matched = re.search(r"(?:安排|创建|约)(.{1,30}?)(?:会议|开会)", user_content)
        return matched.group(1).strip() + "会议" if matched and matched.group(1).strip() else "待确认会议"


def _resolve_time_range(content: str, timezone: ZoneInfo) -> tuple[str, str, str | None]:
    """解析今天/明天的最小时间范围；无法识别时查询未来七天。"""
    now = datetime.now(timezone)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if "今天" in content:
        start = day_start
        end = start + timedelta(days=1)
        requested_date = start.date().isoformat()
    elif "明天" in content:
        start = day_start + timedelta(days=1)
        end = start + timedelta(days=1)
        requested_date = start.date().isoformat()
    else:
        start = now
        end = now + timedelta(days=7)
        requested_date = None
    return start.isoformat(), end.isoformat(), requested_date


def _resolve_duration(content: str) -> int:
    """提取常见会议时长，未指定时使用 30 分钟。"""
    if "半小时" in content:
        return 30
    matched = re.search(r"(\d{1,3})\s*分钟", content)
    if matched:
        return max(15, min(int(matched.group(1)), 480))
    return 30
