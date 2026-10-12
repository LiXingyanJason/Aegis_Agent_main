"""以固定 JSON 文件保存开发日历数据的 Mock Provider。"""

import json
from datetime import datetime
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from aegis_calendar_mcp.schemas.calendar import CalendarEvent, CalendarRequestContext


class MockCalendarProvider:
    """将开发日历数据保存到单一 JSON 文件，重启服务后仍可读取。"""

    def __init__(self, data_path: Path) -> None:
        self._data_path = data_path

    async def list_events(self, context: CalendarRequestContext, start_at: datetime, end_at: datetime) -> list[CalendarEvent]:
        """从固定 JSON 文件读取指定范围内有重叠的事件。"""
        del context
        return [event for event in (_event_from_record(item) for item in self._load()) if event.start_at < end_at and event.end_at > start_at]

    async def create_event(self, context: CalendarRequestContext, event: CalendarEvent, idempotency_key: str) -> str:
        """写回 JSON；相同幂等键返回既有外部事件标识。"""
        del context
        records = self._load()
        for item in records:
            if item.get("idempotency_key") == idempotency_key:
                return str(item["external_event_id"])
        external_event_id = f"mock-event-{uuid5(NAMESPACE_URL, idempotency_key)}"
        records.append({"external_event_id": external_event_id, "idempotency_key": idempotency_key, "title": event.title, "start_at": event.start_at.isoformat(), "end_at": event.end_at.isoformat(), "attendees": list(event.attendees), "description": event.description, "location": event.location})
        self._write(records)
        return external_event_id

    async def get_event(self, context: CalendarRequestContext, external_event_id: str) -> CalendarEvent | None:
        """开发共享日历中按外部 ID 精确读取；仅用于单用户 Mock。"""
        del context
        for record in self._load():
            if record["external_event_id"] == external_event_id:
                return _event_from_record(record)
        return None

    def _load(self) -> list[dict[str, object]]:
        if not self._data_path.exists():
            self._write(_initial_records())
        try:
            payload = json.loads(self._data_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"Mock 日历数据文件不可读取：{self._data_path}") from error
        if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
            raise ValueError("Mock 日历数据必须是事件对象数组")
        changed = False
        for index, item in enumerate(payload):
            if not item.get("external_event_id"):
                # 兼容旧样例：一次补齐并落盘，之后编辑标题或时间也不改变 ID。
                item["external_event_id"] = f"mock-legacy-{uuid5(NAMESPACE_URL, str(self._data_path.resolve()) + ':' + str(index))}"
                changed = True
        ids = [item["external_event_id"] for item in payload]
        if not all(isinstance(value, str) and value for value in ids) or len(set(ids)) != len(ids):
            raise ValueError("Mock 日历事件标识无效或重复")
        if changed:
            self._write(payload)
        return payload

    def _write(self, records: list[dict[str, object]]) -> None:
        self._data_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._data_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self._data_path)


def _event_from_record(item: dict[str, object]) -> CalendarEvent:
    try:
        return CalendarEvent(str(item["title"]), datetime.fromisoformat(str(item["start_at"])), datetime.fromisoformat(str(item["end_at"])), tuple(str(value) for value in item.get("attendees", [])), str(item["external_event_id"]), item.get("description"), item.get("location"))
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("Mock 日历事件格式无效") from error


def _initial_records() -> list[dict[str, object]]:
    """当开发 JSON 尚不存在时写入与仓库样例一致的近期测试日程。"""
    return [
        {"external_event_id": "mock-seed-20261009-standup", "title": "研发站会", "start_at": "2026-10-09T01:00:00+00:00", "end_at": "2026-10-09T01:30:00+00:00", "attendees": ["jason@example.com", "wangmin@example.com", "chenyu@example.com"]},
        {"external_event_id": "mock-seed-20261009-review", "title": "项目阶段评审", "start_at": "2026-10-09T05:00:00+00:00", "end_at": "2026-10-09T06:00:00+00:00", "attendees": ["jason@example.com", "wangmin@example.com"]},
        {"external_event_id": "mock-seed-20261010-sync", "title": "项目同步", "start_at": "2026-10-10T01:00:00+00:00", "end_at": "2026-10-10T01:30:00+00:00", "attendees": ["jason@example.com", "wangmin@example.com"]},
        {"external_event_id": "mock-seed-20261010-planning", "title": "产品迭代规划", "start_at": "2026-10-10T03:00:00+00:00", "end_at": "2026-10-10T04:00:00+00:00", "attendees": ["jason@example.com", "chenyu@example.com"]},
        {"external_event_id": "mock-seed-20261010-renewal", "title": "客户续约沟通", "start_at": "2026-10-10T06:00:00+00:00", "end_at": "2026-10-10T07:00:00+00:00", "attendees": ["jason@example.com", "sales@example.com"]},
        {"external_event_id": "mock-seed-20261012-weekly", "title": "下周工作排期", "start_at": "2026-10-12T02:00:00+00:00", "end_at": "2026-10-12T03:00:00+00:00", "attendees": ["jason@example.com"]},
    ]
