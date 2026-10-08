"""邮件摘要、回复草稿模型输出解析的单元测试。"""

import pytest

from aegis_agent_worker.service.mail_task_service import MailTaskError, _parse_extraction, _parse_reply


def test_parse_extraction_keeps_candidates_and_todos() -> None:
    """测试摘要任务接受约定 JSON，并保留待办和截止时间候选。"""
    result = _parse_extraction(
        '''{
          "key_points": ["客户要求本周确认续约价格"],
          "todos": [{"content": "确认续约报价", "due_at": "2026-10-09T09:00:00+08:00", "due_text": "本周五", "is_inferred": true}],
          "due_date_candidates": ["2026-10-09T09:00:00+08:00"]
        }'''
    )

    assert result["key_points"] == ["客户要求本周确认续约价格"]
    assert result["todos"][0]["content"] == "确认续约报价"
    assert result["todos"][0]["is_inferred"] is True
    assert result["due_date_candidates"] == ["2026-10-09T09:00:00+08:00"]


def test_parse_extraction_rejects_non_list_fields() -> None:
    """测试模型把数组字段误返回为对象时，任务会受控失败而不会写入异常数据。"""
    with pytest.raises(MailTaskError, match="摘要字段格式无效"):
        _parse_extraction('{"key_points": {}, "todos": [], "due_date_candidates": []}')


def test_parse_reply_normalizes_recipients() -> None:
    """测试起草结果去除收件人空白、统一大小写并去重。"""
    result = _parse_reply(
        '''{"to": [" LiLi@northstar.example.com ", "lili@northstar.example.com"],
        "cc": [], "subject": "Re: 续约确认", "body": "您好，报价将在周五前确认。"}'''
    )

    assert result["to"] == ["lili@northstar.example.com"]
    assert result["subject"] == "Re: 续约确认"


def test_parse_reply_rejects_missing_recipient() -> None:
    """测试未给出有效收件人时不创建站内邮件草稿。"""
    with pytest.raises(MailTaskError, match="有效收件人"):
        _parse_reply('{"to": [], "cc": [], "subject": "测试", "body": "正文"}')
