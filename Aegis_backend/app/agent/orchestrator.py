"""最小 Agent 执行编排：读取会话、调用 LLM、保存回复。"""

import asyncio
import logging
from dataclasses import dataclass
from uuid import UUID

from app.config.database import Database, TenantContext, tenant_transaction
from app.llm.client import LLMError, LLMMessage, OpenAICompatibleClient
from app.repository.run_repository import AgentRunRepository

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AgentExecutionResult:
    """Worker 输出所需的任务执行结果；错误信息已在业务层脱敏。"""

    status: str
    error_code: str | None = None
    error_message: str | None = None


class AgentOrchestrator:
    """消费一个 queued 任务并完成第一版文本回复。"""

    _system_prompt = "你是 Aegis PA 个人效率助手。请基于当前会话，给出准确、简洁、可执行的中文回复。"

    def __init__(
        self,
        database: Database,
        llm_client: OpenAICompatibleClient,
        runs: AgentRunRepository | None = None,
    ) -> None:
        self._database = database
        self._llm_client = llm_client
        self._runs = runs or AgentRunRepository()

    async def execute(self, run_id: UUID) -> AgentExecutionResult:
        """执行任务，并返回 `completed`、`failed` 或 `ignored` 供 Worker 输出运行状态。"""
        async with self._database.session() as session:
            async with session.begin():
                run = await self._runs.claim_queued_run(
                    session,
                    run_id,
                    model_provider=self._llm_client.provider_name,
                    model_name=self._llm_client.model_name,
                )
        if run is None:
            return AgentExecutionResult(status="ignored")

        context = TenantContext(tenant_id=run.tenant_id, user_id=run.user_id)
        step_id: UUID | None = None
        try:
            async with self._database.session() as session:
                async with tenant_transaction(session, context):
                    history = await self._runs.load_conversation_history(
                        session,
                        conversation_id=run.conversation_id,
                        tenant_id=run.tenant_id,
                        user_id=run.user_id,
                    )
                    step_id = await self._runs.create_running_llm_step(
                        session, run_id=run.id, tenant_id=run.tenant_id
                    )

            messages = [LLMMessage(role="system", content=self._system_prompt)] + [
                LLMMessage(role=item["role"], content=item["content"]) for item in history
            ]
            reply = await self._llm_client.complete(messages)

            async with self._database.session() as session:
                async with tenant_transaction(session, context):
                    await self._runs.complete_run_with_assistant_message(
                        session,
                        run_id=run.id,
                        conversation_id=run.conversation_id,
                        tenant_id=run.tenant_id,
                        user_id=run.user_id,
                        assistant_content=reply,
                        step_id=step_id,
                    )
            return AgentExecutionResult(status="completed")
        except asyncio.CancelledError:
            raise
        except LLMError as error:
            await self._mark_failed(run, step_id, "LLM_UNAVAILABLE", str(error))
            return AgentExecutionResult(
                status="failed", error_code="LLM_UNAVAILABLE", error_message=str(error)
            )
        except Exception:
            # 仅写入 Worker 本地日志以帮助开发排错；数据库和接口仍使用脱敏错误文本。
            logger.exception("Agent 执行失败：run_id=%s", run.id)
            await self._mark_failed(run, step_id, "AGENT_EXECUTION_FAILED", "任务执行失败")
            return AgentExecutionResult(
                status="failed", error_code="AGENT_EXECUTION_FAILED", error_message="任务执行失败"
            )

    async def _mark_failed(
        self, run, step_id: UUID | None, error_code: str, error_message: str
    ) -> None:
        """在可确定的租户上下文中记录执行失败，避免 Worker 直接向外抛出业务异常。"""
        context = TenantContext(tenant_id=run.tenant_id, user_id=run.user_id)
        async with self._database.session() as session:
            async with tenant_transaction(session, context):
                await self._runs.fail_run(
                    session,
                    run_id=run.id,
                    tenant_id=run.tenant_id,
                    step_id=step_id,
                    error_code=error_code,
                    error_message=error_message,
                )
