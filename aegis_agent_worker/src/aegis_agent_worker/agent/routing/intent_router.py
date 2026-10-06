"""将用户最新消息归类为当前支持的任务意图。"""


class IntentRouter:
    """一期仅做确定性日历读取识别，避免模型绕过受控工具策略。"""

    _calendar_keywords = ("日程", "日历", "行程", "安排", "会议", "空闲", "可用时间")

    def classify(self, user_content: str) -> str:
        """返回 calendar_read 或 general_chat。"""
        normalized = user_content.strip()
        if normalized and any(keyword in normalized for keyword in self._calendar_keywords):
            return "calendar_read"
        return "general_chat"
