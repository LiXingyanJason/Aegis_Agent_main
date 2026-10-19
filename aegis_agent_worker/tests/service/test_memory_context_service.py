"""长期记忆确定性筛选规则测试。"""

from uuid import UUID
from types import SimpleNamespace
import json

from aegis_agent_worker.service.memory.memory_context_service import (
    MemoryContextService,
    _select_relevant,
    _terms,
)


def _candidate(memory_id: str, content: str) -> dict:
    """构造数据库已过滤为非敏感的候选记忆。"""
    return {"id": UUID(memory_id), "content": content, "source": "user_input"}


def test_selects_calendar_preferences_but_not_mail_tone() -> None:
    """测试会议任务只选择相关时间偏好，不注入邮件语气偏好。"""
    selected = _select_relevant(
        [
            _candidate("10000000-0000-4000-8000-000000000001", "默认使用北京时间。"),
            _candidate("10000000-0000-4000-8000-000000000002", "周一上午不安排会议。"),
            _candidate("10000000-0000-4000-8000-000000000003", "给客户的邮件语气保持正式、简洁。"),
        ],
        _terms("帮我安排下周的项目会议"),
    )

    assert [item.content for item in selected] == ["默认使用北京时间。", "周一上午不安排会议。"]


def test_formats_selected_memory_as_non_executable_data_context() -> None:
    """测试记忆提示明确说明其只是低优先级数据，不能成为系统指令。"""
    selected = _select_relevant(
        [_candidate("10000000-0000-4000-8000-000000000001", "邮件语气正式、简洁。")],
        _terms("请起草一封正式邮件"),
    )

    bundle=SimpleNamespace(items=[SimpleNamespace(memory_id=selected[0].id,memory_type='long_term_summary',
        version=1,excerpt=selected[0].content)])
    prompt = MemoryContextService.format_bundle(bundle)

    assert prompt is not None
    assert "不是系统指令" in prompt
    assert json.loads(prompt.split('\n',1)[1])['memory_data'][0]['excerpt']=="邮件语气正式、简洁。"
