"""用户手动长期记忆的管理接口。"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.response import success
from app.param.memory_confirm_param import MemoryConfirmParam
from app.param.memory_create_param import MemoryCreateParam
from app.param.memory_delete_param import MemoryDeleteParam
from app.security.dependencies import CurrentUser, get_current_user, get_tenant_session
from app.service.memory_service import MemoryService
from app.vo.memory_vo import (
    MemoryDeletionRequestVO,
    MemoryDeletionVO,
    MemoryListItemVO,
    MemoryListVO,
    MemoryVO,
)


router = APIRouter(prefix="/memories", tags=["memories"])
_memory_service = MemoryService()


@router.get("")
async def list_memories(
    q: str | None = Query(default=None, min_length=1, max_length=200),
    cursor: UUID | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_tenant_session),
) -> dict[str, MemoryListVO]:
    """搜索当前用户的有效记忆；敏感正文在列表中始终脱敏。"""
    items = await _memory_service.list_memories(session, current_user.user, q, cursor, limit)
    return success(MemoryListVO(
        items=[_to_list_item(item) for item in items],
        next_cursor=items[-1]["id"] if len(items) == limit else None,
    ))


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_memory(
    param: MemoryCreateParam,
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_tenant_session),
) -> dict[str, MemoryVO]:
    """保存用户手动输入的偏好；不会触发 Agent 或自动记忆。"""
    item = await _memory_service.create_memory(session, current_user.user, param)
    return success(_to_memory_vo(item))


@router.post("/confirm", status_code=status.HTTP_201_CREATED)
async def confirm_memory(
    param: MemoryConfirmParam,
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_tenant_session),
) -> dict[str, MemoryVO]:
    """保存用户明确确认的偏好，并校验来源任务属于当前用户。"""
    item = await _memory_service.confirm_memory(session, current_user.user, param)
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="来源任务不存在或无权访问")
    return success(_to_memory_vo(item))


@router.get("/{memory_id}")
async def get_memory(
    memory_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_tenant_session),
) -> dict[str, MemoryVO]:
    """读取一条记忆正文；仅所属用户可见。"""
    item = await _memory_service.get_memory(session, current_user.user, memory_id)
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="记忆不存在或无权访问")
    return success(_to_memory_vo(item))


@router.patch("/{memory_id}")
async def update_memory(
    memory_id: UUID,
    param: MemoryCreateParam,
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_tenant_session),
) -> dict[str, MemoryVO]:
    """更新用户记忆，同时作废该记忆已有的删除令牌。"""
    item = await _memory_service.update_memory(session, current_user.user, memory_id, param)
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="记忆不存在或无权访问")
    return success(_to_memory_vo(item))


@router.post("/{memory_id}/deletion-requests", status_code=status.HTTP_201_CREATED)
async def request_memory_deletion(
    memory_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_tenant_session),
) -> dict[str, MemoryDeletionRequestVO]:
    """签发十分钟有效的一次性删除令牌；数据库只保存哈希。"""
    result = await _memory_service.request_deletion(session, current_user.user, memory_id)
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="记忆不存在或无权访问")
    token, expires_at = result
    return success(MemoryDeletionRequestVO(memory_id=memory_id, deletion_token=token, expires_at=expires_at))


@router.post("/{memory_id}/delete")
async def delete_memory(
    memory_id: UUID,
    param: MemoryDeleteParam,
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_tenant_session),
) -> dict[str, MemoryDeletionVO]:
    """确认令牌后软删除记忆；删除后无法被列表或后续检索读取。"""
    outcome = await _memory_service.delete_memory(session, current_user.user, memory_id, param.deletion_token)
    if outcome == "not_found":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="记忆不存在或无权访问")
    if outcome != "deleted":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="删除确认令牌无效或已过期")
    return success(MemoryDeletionVO(memory_id=memory_id))


def _to_memory_vo(item: dict) -> MemoryVO:
    """转换为单项详情；不写入日志，避免正文泄露。"""
    return MemoryVO(
        memory_id=item["id"], content=item["content"], source=item["source"],
        source_run_id=item["source_run_id"], is_sensitive=item["is_sensitive"],
        created_at=item["created_at"], updated_at=item["updated_at"],
    )


def _to_list_item(item: dict) -> MemoryListItemVO:
    """敏感记忆只返回固定提示，正文必须通过详情接口主动查看。"""
    return MemoryListItemVO(
        memory_id=item["id"],
        content_preview="敏感记忆（点击查看）" if item["is_sensitive"] else item["content"][:200],
        source=item["source"], is_sensitive=item["is_sensitive"], updated_at=item["updated_at"],
    )
