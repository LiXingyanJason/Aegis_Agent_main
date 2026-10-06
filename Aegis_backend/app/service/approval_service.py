"""逐项确认的业务规则。"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.param.approval_decision_param import ApprovalDecisionParam
from app.repository.approval_repository import ApprovalDecisionResult, ApprovalRepository
from app.repository.user_repository import LocalUser


class ApprovalService:
    """处理当前用户对待确认外部操作的批准或拒绝。"""

    def __init__(self, approvals: ApprovalRepository | None = None) -> None:
        self._approvals = approvals or ApprovalRepository()

    async def decide(self, session: AsyncSession, local_user: LocalUser, approval_item_id: UUID, param: ApprovalDecisionParam) -> ApprovalDecisionResult:
        """在 RLS 事务内保存决定；暂不调度任何外部写操作。"""
        return await self._approvals.decide(session, approval_item_id=approval_item_id, tenant_id=local_user.tenant_id, user_id=local_user.id, decision=param.decision, reason=param.reason)

    async def list_items(self, session: AsyncSession, local_user: LocalUser, run_id: UUID | None, status: str | None) -> list[dict]:
        """为确认页加载当前用户的待确认或历史项目。"""
        return await self._approvals.list_for_user(session, tenant_id=local_user.tenant_id, user_id=local_user.id, run_id=run_id, status=status)

    async def get_item(self, session: AsyncSession, local_user: LocalUser, approval_item_id: UUID) -> dict | None:
        """返回单个确认项的权威预览。"""
        return await self._approvals.find_for_user(session, approval_item_id=approval_item_id, tenant_id=local_user.tenant_id, user_id=local_user.id)
