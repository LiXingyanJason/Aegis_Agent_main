"""Embedding 治理适配：独立策略／主体，共享租户事务、超时和稳定错误体系。"""
from contextlib import asynccontextmanager
from dataclasses import dataclass
from time import monotonic
from uuid import UUID
from aegis_agent_worker.config.database import TenantContext, tenant_transaction
from aegis_agent_worker.harness.exceptions import HarnessError
from aegis_agent_worker.harness.policies.embedding import EmbeddingPolicyProvider
from aegis_agent_worker.harness.timeout import execution_timeout
from aegis_agent_worker.observability.telemetry import get_tracer
from aegis_agent_worker.repository.embedding_repository import EmbeddingRepository


@dataclass(frozen=True, slots=True)
class EmbeddingSubject:
    kind: str
    id: UUID
    tenant_id: UUID
    user_id: UUID

    def __post_init__(self):
        if self.kind not in {"run", "job"} or any(not isinstance(value, UUID)
            for value in (self.id, self.tenant_id, self.user_id)):
            raise HarnessError("EMBEDDING_CONTEXT_REQUIRED", "需要真实任务或索引任务身份")

    @property
    def purpose(self):
        return "knowledge_index" if self.kind == "job" else "knowledge_query"


class EmbeddingExecutor:
    def __init__(self, database, client, *, repository=None, policies=None, external_queries_allowed=False):
        self._database, self._client = database, client
        self._repository = repository or EmbeddingRepository()
        self._policies = policies or EmbeddingPolicyProvider()
        self._external_queries_allowed = external_queries_allowed

    @asynccontextmanager
    async def _session(self, subject):
        async with self._database.session() as session:
            async with tenant_transaction(session, TenantContext(subject.tenant_id, subject.user_id)):
                yield session

    async def embed(self, subject: EmbeddingSubject, texts: list[str]):
        """唯一业务调用入口；额度占用提交后才调用网络，失败不返还已占用预算。"""
        if not isinstance(subject, EmbeddingSubject):
            raise HarnessError("EMBEDDING_CONTEXT_REQUIRED", "缺少可信 Embedding 主体")
        self._client.ensure_configured()
        if subject.kind == "run" and not self._external_queries_allowed:
            raise HarnessError("EMBEDDING_DATA_POLICY_BLOCKED", "查询文本尚未获外发授权")
        if not isinstance(texts, list) or not texts or any(not isinstance(value, str) or not value.strip() for value in texts):
            raise HarnessError("EMBEDDING_INPUT_INVALID", "Embedding 输入不能为空")
        chars = sum(len(value) for value in texts)
        profile = self._client.profile
        async with self._session(subject) as session:
            await self._repository.authorize(session, subject, profile)
            budget = await self._repository.ensure_budget(session, subject, profile,
                self._policies.current(subject.purpose).version)
            policy = self._policies.resolve(budget["policy_version"])
            if policy.purpose != subject.purpose:
                raise HarnessError("EMBEDDING_POLICY_UNAVAILABLE", "Embedding 用途与策略不匹配")
            if len(texts) > policy.max_batch_items or chars > policy.max_batch_chars:
                raise HarnessError("EMBEDDING_INPUT_LIMIT", "Embedding 批次超过上限")
            await self._repository.reserve(session, budget["id"], subject, policy, chars)
            attempt_id = await self._repository.start(session, budget["id"], subject, chars, len(texts))
        self._client.reset_usage()
        started, status, code = monotonic(), "succeeded", None
        try:
            with get_tracer(__name__).start_as_current_span("embedding.invoke") as span:
                span.set_attribute("aegis.embedding.purpose", subject.purpose)
                span.set_attribute("aegis.embedding.subject_id", str(subject.id))
                async with execution_timeout(policy.timeout_seconds, "EMBEDDING_TIMEOUT", "Embedding 执行超时"):
                    return await self._client.embed(texts)
        except BaseException as error:
            status, code = "failed", getattr(error, "code", "EMBEDDING_EXECUTION_FAILED")
            raise
        finally:
            async with self._session(subject) as session:
                await self._repository.finish(session, attempt_id, subject, status=status, code=code,
                    duration_ms=round((monotonic()-started)*1000), usage=self._client.usage)
