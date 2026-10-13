"""邮件结构化输出的有界修复与失败关闭测试。"""
import pytest

from aegis_agent_worker.agent.specialists.mail_agent import MailAgent, MailAgentError
from aegis_agent_worker.llm.client import LLMError, LLMOutputError
from aegis_agent_worker.harness.exceptions import HarnessError


class Client:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    async def complete(self, messages):
        raise AssertionError("结构化路径必须使用 JSON mode")

    async def complete_json(self, messages):
        self.calls.append(messages)
        value = next(self.responses)
        if isinstance(value, Exception):
            raise value
        return value


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [
    "我将开始起草回复", "", "```json", '["generate_draft"]',
    '{"action":"call_tool","tool_name":"mail.messages.send"}',
    '{"action":"call_tool","tool_name":"calendar.list_events","arguments":{}}',
    LLMOutputError("模型服务未返回有效文本"),
])
async def test_invalid_collection_output_repairs_once(invalid):
    """非JSON、空响应、非法工具或参数均重新校验，修复成功后才返回决定。"""
    client = Client([invalid, '{"action":"generate_draft"}'])
    decision = await MailAgent(client).collect_information(source={}, evidence=[], answers=[])
    assert decision.action == "generate_draft" and len(client.calls) == 2
    assert client.calls[1][-1].role == "system"
    assert "上一次结果" in client.calls[1][-1].content


@pytest.mark.asyncio
async def test_two_invalid_outputs_fail_closed_without_leaking_response():
    """连续失败不默认生成草稿，不泄露响应，也不进行第三次模型调用。"""
    client = Client(["PRIVATE INVALID RESPONSE", '{"action":"send"}'])
    with pytest.raises(MailAgentError) as error:
        await MailAgent(client).collect_information(source={}, evidence=[], answers=[])
    assert error.value.code == "MODEL_OUTPUT_INVALID"
    assert "PRIVATE" not in str(error.value) and len(client.calls) == 2
    assert "PRIVATE" not in client.calls[1][-1].content


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [LLMError("网络故障"), HarnessError("EXECUTION_BUDGET_EXCEEDED", "预算耗尽")])
async def test_network_or_budget_errors_do_not_trigger_output_repair(failure):
    """服务故障或预算耗尽不是格式错误，不能利用修复绕过调用上限。"""
    client = Client([failure])
    with pytest.raises(type(failure)):
        await MailAgent(client).collect_information(source={}, evidence=[], answers=[])
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_body_revision_repairs_unknown_fields_without_relaxing_contract():
    """正文修改首次携带收件人字段被拒绝，重试仍必须只返回正文。"""
    client = Client(['{"body":"正文","to":["bad@example.com"]}', '{"body":"正式正文"}'])
    assert await MailAgent(client).revise_body("原正文", "正式一些") == "正式正文"
    assert len(client.calls) == 2
