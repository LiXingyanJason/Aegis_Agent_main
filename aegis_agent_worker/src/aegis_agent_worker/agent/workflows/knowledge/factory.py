"""知识领域依赖由Factory组装；生产能力默认不开放假检索。"""
from dataclasses import dataclass
from aegis_agent_worker.agent.contracts.decision import ConversationCapability
from aegis_agent_worker.agent.workflows.knowledge.query_workflow import KnowledgeQueryWorkflow
from aegis_agent_worker.service.knowledge.knowledge_query_service import KnowledgeQueryService


@dataclass(frozen=True)
class KnowledgeWorkflowFactory:
    agent: object
    query_service: KnowledgeQueryService | None = None
    evidence_service: object | None = None
    artifact_service: object | None = None
    enabled: bool = False

    def register(self, registry):
        registry.register("knowledge_query", KnowledgeQueryWorkflow(
            self.agent, self.query_service, self.evidence_service, self.artifact_service).build())
        if self.enabled:
            registry.register_capability(ConversationCapability(domain="knowledge", intent="query_knowledge",
                workflow="knowledge_query", description="检索已授权公司政策、合同、会议纪要或项目资料并附来源；未配置则明确不可用"))
