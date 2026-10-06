"""将意图映射到根图已注册的工作流名称。"""


class WorkflowRouter:
    """集中维护意图到工作流的显式白名单映射。"""

    _mapping = {"calendar_read": "calendar_read", "general_chat": "general_reply"}

    def select(self, intent: str) -> str:
        """未知意图安全地回退到普通回复流程。"""
        return self._mapping.get(intent, "general_reply")
