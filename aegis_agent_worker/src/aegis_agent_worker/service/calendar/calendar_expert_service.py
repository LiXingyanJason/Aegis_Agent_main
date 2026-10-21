"""日历专家只读业务动作，复用既有工具服务，不调用LLM。"""
from datetime import datetime, timedelta

from aegis_agent_worker.agent.contracts.calendar import CalendarDecision, extract_calendar_free_time_candidates
from aegis_agent_worker.agent.contracts.cooperation.mail import MailMeetingRequirements
from aegis_agent_worker.config.database import TenantContext
from aegis_agent_worker.service.calendar.calendar_confirmation_service import explicit_interval_availability
from aegis_agent_worker.tool.contracts import ToolError


class CalendarExpertService:
    def __init__(self, tool_execution):
        self._tools = tool_execution

    async def query(self, run, requirements, window_index, connection_id=None):
        requirements = MailMeetingRequirements.model_validate(requirements)
        if not 0 <= window_index < len(requirements.time_windows):
            raise ToolError("CALENDAR_WINDOW_INVALID", "未提供该时间窗口")
        window = requirements.time_windows[window_index]
        explicit = window.end_at-window.start_at == timedelta(minutes=requirements.duration_minutes)
        decision = CalendarDecision(action="prepare_meeting", timezone=requirements.timezone,
            start_at=window.start_at, end_at=window.end_at, duration_minutes=requirements.duration_minutes,
            time_mode="explicit_interval" if explicit else "search_window",
            title=requirements.title, attendees=list(requirements.attendees))
        return await self._read(run, decision, connection_id)

    async def _read(self, run, decision, connection_id):
        result = await self._tools.execute_read_tool(run, TenantContext(run.tenant_id,run.user_id),
            decision.to_read_plan().to_invocation(), required_connection_id=connection_id)
        if result.status != "succeeded":
            raise ToolError(result.error_code or "CALENDAR_TOOL_UNAVAILABLE", "日历查询不可用")
        if result.connection_id is None or (connection_id is not None and result.connection_id != connection_id):
            raise ToolError("CALENDAR_CONNECTION_CHANGED", "查询日历连接发生变化")
        summary = result.output_summary
        if decision.time_mode == "explicit_interval":
            summary = explicit_interval_availability(decision, summary)
        if not isinstance(summary, dict) or not isinstance(summary.get("available_slots"), list):
            raise ToolError("CALENDAR_INVALID_RESPONSE", "未取得合法候选集合")
        candidates, duration = extract_calendar_free_time_candidates(summary)
        if duration != decision.duration_minutes:
            raise ToolError("CALENDAR_INVALID_RESPONSE", "候选时长与请求不一致")
        if len(candidates) != min(10, len(summary["available_slots"])):
            raise ToolError("CALENDAR_INVALID_RESPONSE", "存在无效候选")
        for item in candidates:
            start, end = datetime.fromisoformat(item.start_at), datetime.fromisoformat(item.end_at)
            if (start.tzinfo is None or end.tzinfo is None or not decision.start_at <= start < end <= decision.end_at
                    or end-start < timedelta(minutes=duration)):
                raise ToolError("CALENDAR_INVALID_RESPONSE", "候选越界或不足会议时长")
        return dict(connection_id=str(result.connection_id), candidates=[item.model_dump(mode="json") for item in candidates],
                    status="available" if candidates else "empty", duration_minutes=duration,
                    truncated=len(summary["available_slots"])>len(candidates),
                    scheduling_warning="仅核验当前用户授权日历；未证明其他参会人空闲。")

    async def check_selected(self, run, requirements, candidate, connection_id):
        start = datetime.fromisoformat(candidate.start_at)
        decision = CalendarDecision(action="prepare_meeting", time_mode="explicit_interval",
            start_at=start, end_at=start+timedelta(minutes=requirements.duration_minutes),
            timezone=requirements.timezone, duration_minutes=requirements.duration_minutes,
            title=requirements.title, attendees=list(requirements.attendees))
        return bool((await self._read(run, decision, connection_id))["candidates"])
