"""工作流的风险决策预留。"""


class RiskRouter:
    """一期只读日历工具无需审批；写操作接入后统一在此判断。"""

    def requires_approval(self, workflow: str) -> bool:
        """返回工作流是否必须进入人工确认。"""
        return workflow in {"calendar_confirmation", "mail_send_confirmation"}
