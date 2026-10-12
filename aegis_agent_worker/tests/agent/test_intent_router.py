"""会话输入到工作流的受控路由测试。"""

from aegis_agent_worker.agent.routing.intent_router import IntentRouter


def test_meeting_request_with_duration_routes_to_confirmation() -> None:
    """测试自然语言的会议安排不会误走只读日历查询流程。"""
    router = IntentRouter()

    assert router.classify("安排一个50分钟的会议") == "calendar_confirmation"


def test_availability_question_without_write_intent_routes_to_read() -> None:
    """测试只问空闲时间的请求仍使用只读日历工具。"""
    router = IntentRouter()

    assert router.classify("查询明天有哪些空闲时间") == "calendar_read"
