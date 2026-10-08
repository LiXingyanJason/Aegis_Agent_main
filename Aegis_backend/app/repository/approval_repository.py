"""待确认操作及确认决定的数据访问。"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from hashlib import sha256
from secrets import token_urlsafe
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True, slots=True)
class ApprovalDecisionResult:
    """确认操作的受控结果；状态供 Service 转换为 HTTP 响应。"""

    outcome: str
    approval_item_id: UUID | None = None
    run_id: UUID | None = None
    approval_status: str | None = None


class ApprovalRepository:
    """在当前租户事务中锁定并更新单个 approval_item。"""

    async def decide(
        self,
        session: AsyncSession,
        *,
        approval_item_id: UUID,
        tenant_id: UUID,
        user_id: UUID,
        decision: str,
        reason: str | None,
    ) -> ApprovalDecisionResult:
        """仅允许拥有者对尚未过期的 pending 项目作出一次决定。"""
        result = await session.execute(
            text(
                "SELECT ai.id, ai.run_id, ai.status, ai.expires_at < now() AS expired, "
                "ar.run_type, ar.status AS run_status "
                "FROM approval_items ai JOIN agent_runs ar ON ar.id = ai.run_id "
                "WHERE ai.id = :approval_item_id AND ai.tenant_id = :tenant_id "
                "AND ai.user_id = :user_id FOR UPDATE OF ai, ar"
            ),
            {"approval_item_id": approval_item_id, "tenant_id": tenant_id, "user_id": user_id},
        )
        item = result.mappings().first()
        if item is None:
            return ApprovalDecisionResult(outcome="not_found")
        if item["status"] != "pending":
            return ApprovalDecisionResult(outcome="not_pending", approval_item_id=item["id"], run_id=item["run_id"], approval_status=item["status"])
        # 邮件发送确认项由 Worker 先运行到 LangGraph interrupt 并保存 Checkpoint，
        # 在此之前接受决定会导致 resume 找不到对应图状态。
        if item["run_type"] == "mail_send" and item["run_status"] != "waiting_confirmation":
            return ApprovalDecisionResult(
                outcome="workflow_not_ready",
                approval_item_id=item["id"],
                run_id=item["run_id"],
                approval_status=item["status"],
            )
        if item["expired"]:
            await session.execute(
                text("UPDATE approval_items SET status = 'expired', updated_at = now() WHERE id = :id"),
                {"id": item["id"]},
            )
            return ApprovalDecisionResult(outcome="expired", approval_item_id=item["id"], run_id=item["run_id"], approval_status="expired")

        approval_status = "approved_executing" if decision == "approved" else "rejected"
        # 当前阶段只有登录用户本人可操作；此哈希是一次确认决定的审计凭据。
        token_hash = sha256(token_urlsafe(32).encode("utf-8")).hexdigest() if decision == "approved" else None
        await session.execute(
            text(
                "INSERT INTO approval_decisions "
                "(tenant_id, approval_item_id, actor_user_id, decision, checked, reason, approval_token_hash, token_issued_at, token_used_at) "
                "VALUES (:tenant_id, :approval_item_id, :user_id, :decision, :checked, :reason, :token_hash, "
                "CASE WHEN :checked THEN now() ELSE NULL END, CASE WHEN :checked THEN now() ELSE NULL END)"
            ),
            {"tenant_id": tenant_id, "approval_item_id": item["id"], "user_id": user_id, "decision": decision, "checked": decision == "approved", "reason": reason, "token_hash": token_hash},
        )
        await session.execute(
            text("UPDATE approval_items SET status = :status, updated_at = now() WHERE id = :id"),
            {"id": item["id"], "status": approval_status},
        )
        return ApprovalDecisionResult(outcome="decided", approval_item_id=item["id"], run_id=item["run_id"], approval_status=approval_status)

    async def list_for_user(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        user_id: UUID,
        run_id: UUID | None,
        status: str | None,
    ) -> list[dict[str, Any]]:
        """返回确认页可展示的项目摘要；所有条件始终限定在当前用户。"""
        result = await session.execute(
            text(
                "SELECT id, run_id, action, risk_level, resource_type, resource_id, title, "
                "preview_snapshot, draft_version, status, expires_at, created_at "
                "FROM approval_items WHERE tenant_id = :tenant_id AND user_id = :user_id "
                # asyncpg 在 run_id=None 时不能从 `:run_id IS NULL` 推断参数类型；
                # 显式转换为 uuid，保证“全部任务”列表也能正常查询。
                "AND (CAST(:run_id AS uuid) IS NULL OR run_id = CAST(:run_id AS uuid)) "
                # 同理，为可选状态过滤显式指定 varchar，避免 asyncpg 对 NULL 参数歧义。
                "AND (CAST(:status AS varchar) IS NULL OR status = CAST(:status AS varchar)) "
                "ORDER BY created_at"
            ),
            {"tenant_id": tenant_id, "user_id": user_id, "run_id": run_id, "status": status},
        )
        return [dict(row) for row in result.mappings().all()]

    async def find_for_user(
        self, session: AsyncSession, *, approval_item_id: UUID, tenant_id: UUID, user_id: UUID
    ) -> dict[str, Any] | None:
        """读取一个确认项的创建时权威预览，避免前端篡改草稿参数。"""
        result = await session.execute(
            text(
                "SELECT id, run_id, action, risk_level, resource_type, resource_id, title, "
                "preview_snapshot, parameters_hash, draft_version, status, expires_at, created_at "
                "FROM approval_items WHERE id = :approval_item_id "
                "AND tenant_id = :tenant_id AND user_id = :user_id"
            ),
            {"approval_item_id": approval_item_id, "tenant_id": tenant_id, "user_id": user_id},
        )
        row = result.mappings().first()
        return dict(row) if row is not None else None
