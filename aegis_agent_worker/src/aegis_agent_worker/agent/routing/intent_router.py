"""将用户最新消息归类为当前支持的任务意图。"""

import re


class IntentRouter:
    """用受控规则识别日历读取与需要确认的会议安排。"""

    _calendar_keywords = ("日程", "日历", "行程", "安排", "会议", "空闲", "可用时间")
    _meeting_confirmation_keywords = ("安排会议", "创建会议", "约个会", "约会议", "发会议邀请")
    # 支持“安排一个 50 分钟的会议”“帮我创建下周例会”等自然表达；
    # 仅命中时进入会产生草稿、并要求人工确认的写入工作流。
    _meeting_confirmation_patterns = (
        re.compile(r"(?:安排|创建|预约|约定|发起).{0,30}(?:会议|开会|例会|会)"),
        re.compile(r"(?:帮我|请|麻烦).{0,20}(?:约).{0,20}(?:会议|会)"),
    )

    def classify(self, user_content: str) -> str:
        """返回 calendar_read 或 general_chat。"""
        normalized = user_content.strip()
        if normalized and (
            any(keyword in normalized for keyword in self._meeting_confirmation_keywords)
            or any(pattern.search(normalized) for pattern in self._meeting_confirmation_patterns)
        ):
            return "calendar_confirmation"
        if normalized and any(keyword in normalized for keyword in self._calendar_keywords):
            return "calendar_read"
        return "general_chat"
