"""人工审批恢复后的分支规则预留。"""


class ApprovalRouter:
    """将明确的审批结果转换为图中的后续节点名。"""

    def select(self, decision: str) -> str:
        """批准继续执行，其他状态均安全结束。"""
        return "execute" if decision == "approved" else "rejected"
