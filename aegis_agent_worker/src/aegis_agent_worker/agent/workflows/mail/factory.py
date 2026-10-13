"""邮件领域工作流的组装工厂。"""

from dataclasses import dataclass
from aegis_agent_worker.agent.contracts.decision import ConversationCapability

from aegis_agent_worker.agent.graph.graph_registry import GraphRegistry
from aegis_agent_worker.agent.specialists.mail_agent import MailAgent
from aegis_agent_worker.agent.workflows.mail.extraction_graph import MailExtractionWorkflow
from aegis_agent_worker.agent.workflows.mail.reply_draft_graph import MailReplyDraftWorkflow
from aegis_agent_worker.agent.workflows.mail.send_confirmation_graph import MailSendConfirmationWorkflow
from aegis_agent_worker.service.mail.mail_send_service import MailSendService
from aegis_agent_worker.service.mail.mail_task_service import MailTaskService
from aegis_agent_worker.service.memory.memory_context_service import MemoryContextService
from aegis_agent_worker.agent.workflows.mail.query_workflow import MailQueryWorkflow
from aegis_agent_worker.agent.workflows.mail.revision_workflow import MailRevisionWorkflow


@dataclass(frozen=True, slots=True)
class MailWorkflowFactory:
    """仅由邮件领域决定哪些子图可被根图路由。"""

    mail_tasks: MailTaskService
    mail_send: MailSendService
    mail_agent: MailAgent
    memory_context: MemoryContextService | None = None
    query_service: object | None = None
    artifact_service: object | None = None
    planning_audit: object | None = None
    collection_service: object | None = None
    input_requests: object | None = None
    revision_service: object | None = None

    def register(self, registry: GraphRegistry) -> None:
        """注册邮件派生任务与确认发送子图。"""
        if self.revision_service is not None:
            registry.register("mail_draft_revision",MailRevisionWorkflow(self.mail_agent,self.revision_service).build())
        if self.query_service is not None and self.artifact_service is not None:
            registry.register("mail_query", MailQueryWorkflow(self.mail_agent, self.query_service,
                                                             self.artifact_service, self.planning_audit).build())
            registry.register_capability(ConversationCapability(
                domain="mail", intent="query_mail", workflow="mail_query",
                description="浏览或定位邮件操作卡片；即使用户要求摘要或起草，也只展示卡片供点击"))
        registry.register(
            "mail_extraction", MailExtractionWorkflow(self.mail_tasks, self.mail_agent, self.planning_audit).build()
        )
        registry.register(
            "mail_reply_draft",
            MailReplyDraftWorkflow(
                self.mail_tasks, self.mail_agent, self.memory_context, self.planning_audit,
                self.collection_service,self.input_requests,
            ).build(),
        )
        registry.register(
            "mail_send_confirmation", MailSendConfirmationWorkflow(self.mail_send).build()
        )
        # 摘要／起草子图仍注册供页面明确任务使用，不开放给自然语言对话路由。
