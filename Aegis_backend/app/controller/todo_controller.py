"""Aegis 站内待办计划 HTTP 接口。"""

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.response import success
from app.param.todo_update_param import TodoUpdateParam
from app.security.dependencies import CurrentUser, get_current_user, get_tenant_session
from app.service.todo_service import TodoService
from app.vo.todo_vo import TodoVO


router = APIRouter(prefix="/todos", tags=["todos"])
_todo_service = TodoService()


@router.get("")
async def list_todos(
    status_filter: Literal["draft", "completed", "discarded"] | None = Query(
        default=None, alias="status"
    ),
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_tenant_session),
) -> dict[str, list[TodoVO]]:
    """列出待办计划；不传 status 时返回未丢弃的项目。"""
    items = await _todo_service.list_todos(session, current_user.user, status_filter)
    return success([_to_todo_vo(item) for item in items])


@router.get("/{todo_id}")
async def get_todo(
    todo_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_tenant_session),
) -> dict[str, TodoVO]:
    """读取一项待办，供页面侧边栏或编辑弹窗使用。"""
    item = await _todo_service.get_todo(session, current_user.user, todo_id)
    if item is None:
        raise HTTPException(status_code=404, detail="待办不存在或无权访问")
    return success(_to_todo_vo(item))


@router.patch("/{todo_id}")
async def update_todo(
    todo_id: UUID,
    param: TodoUpdateParam,
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_tenant_session),
) -> dict[str, TodoVO]:
    """编辑站内待办或标记完成/丢弃，不会写入任何外部服务。"""
    item = await _todo_service.update_todo(session, current_user.user, todo_id, param)
    if item is None:
        raise HTTPException(status_code=404, detail="待办不存在或无权访问")
    return success(_to_todo_vo(item))


def _to_todo_vo(item: dict) -> TodoVO:
    """将数据访问层行数据转换为公开的待办页面模型。"""
    return TodoVO(
        todo_id=item["id"],
        email_id=item["email_id"],
        extraction_id=item["extraction_id"],
        content=item["content"],
        due_at=item["due_at"],
        status=item["status"],
        completed_at=item["completed_at"],
        source_subject=item["source_subject"],
        source_sender=item["source_sender"],
        created_at=item["created_at"],
        updated_at=item["updated_at"],
    )
