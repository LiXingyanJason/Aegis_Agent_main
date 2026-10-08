"""待确认外部操作的逐项确认接口。"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.response import success
from app.param.approval_decision_param import ApprovalDecisionParam
from app.security.dependencies import CurrentUser, get_current_user, get_tenant_session
from app.service.approval_service import ApprovalService
from app.service.run_dispatch_service import RunDispatchService, get_run_dispatcher_from_request
from app.vo.approval_vo import ApprovalDecisionVO, ApprovalItemVO

router = APIRouter(prefix="/approvals", tags=["approvals"])
_approval_service = ApprovalService()


def _to_item_vo(item: dict) -> ApprovalItemVO:
    """将数据库行转换为确认页可直接渲染的权威预览。"""
    return ApprovalItemVO(
        approval_item_id=item["id"], run_id=item["run_id"], action=item["action"],
        risk_level=item["risk_level"], resource_type=item["resource_type"],
        resource_id=item["resource_id"], title=item["title"],
        preview_snapshot=item["preview_snapshot"], draft_version=item["draft_version"],
        status=item["status"], expires_at=item["expires_at"], created_at=item["created_at"],
    )


@router.get("")
async def list_approval_items(
    run_id: UUID | None = Query(default=None),
    status_filter: str | None = Query(default="pending", alias="status"),
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_tenant_session),
) -> dict[str, list[ApprovalItemVO]]:
    """按任务加载当前用户的逐项确认列表；默认只返回 pending 项。"""
    items = await _approval_service.list_items(session, current_user.user, run_id, status_filter)
    return success([_to_item_vo(item) for item in items])


@router.get("/{approval_item_id}")
async def get_approval_item(
    approval_item_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_tenant_session),
) -> dict[str, ApprovalItemVO]:
    """
    加载一个确认项在创建时冻结的完整预览。
    默认只返回待确认项，但接口能力不只限于待确认项GET /approvals?status=executed status=rejected status=failed status=approved_executing
    """
    item = await _approval_service.get_item(session, current_user.user, approval_item_id)
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="待确认操作不存在或无权访问")
    return success(_to_item_vo(item))


@router.post("/{approval_item_id}/decision")
async def decide_approval(
        approval_item_id: UUID,
        param: ApprovalDecisionParam,
        current_user: CurrentUser = Depends(get_current_user),
        session: AsyncSession = Depends(get_tenant_session),
        dispatcher: RunDispatchService = Depends(get_run_dispatcher_from_request)
) -> dict[str, ApprovalDecisionVO]:
    """提交决定；事务提交后才投递 Worker 恢复同一 LangGraph 任务。"""
    result = await _approval_service.decide(session, current_user.user, approval_item_id, param) # 执行审批确认的业务逻辑 批准approved_executing拒绝时改为 rejected
    if result.outcome == "not_found":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="待确认操作不存在或无权访问")
    if result.outcome == "expired":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="待确认操作已过期")
    if result.outcome == "workflow_not_ready":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="确认流程正在初始化，请等待任务进度显示“等待发送确认”后重试",
        )
    if result.outcome == "not_pending":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="待确认操作已处理，不能重复确认")
    assert result.approval_item_id is not None and result.run_id is not None and result.approval_status is not None
    dispatcher.enqueue_after_commit(result.run_id, resume_decision=param.decision) # 先创建该入redis队列申请，等待提交数据库事务后，再真正入队
    return success(ApprovalDecisionVO(approval_item_id=result.approval_item_id, run_id=result.run_id, decision=param.decision, approval_status=result.approval_status, execution_scheduled=param.decision == "approved"))
