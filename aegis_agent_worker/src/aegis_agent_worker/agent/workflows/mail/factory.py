"""邮件领域工作流的组装工厂。"""

from dataclasses import dataclass

from aegis_agent_worker.agent.graph.graph_registry import GraphRegistry
from aegis_agent_worker.agent.specialists.mail_agent import MailAgent
from aegis_agent_worker.agent.workflows.mail.extraction_graph import MailExtractionWorkflow
from aegis_agent_worker.agent.workflows.mail.reply_draft_graph import MailReplyDraftWorkflow
from aegis_agent_worker.agent.workflows.mail.send_confirmation_graph import MailSendConfirmationWorkflow
from aegis_agent_worker.service.mail.mail_send_service import MailSendService
from aegis_agent_worker.service.mail.mail_task_service import MailTaskService
from aegis_agent_worker.service.memory.memory_context_service import MemoryContextService


@dataclass(frozen=True, slots=True)
class MailWorkflowFactory:
    """仅由邮件领域决定哪些子图可被根图路由。"""

    mail_tasks: MailTaskService
    mail_send: MailSendService
    mail_agent: MailAgent
    memory_context: MemoryContextService | None = None

    def register(self, registry: GraphRegistry) -> None:
        """注册邮件派生任务与确认发送子图。"""
        registry.register(
            "mail_extraction", MailExtractionWorkflow(self.mail_tasks, self.mail_agent).build()
        )
        registry.register(
            "mail_reply_draft",
            MailReplyDraftWorkflow(
                self.mail_tasks, self.mail_agent, self.memory_context
            ).build(),
        )
        registry.register(
            "mail_send_confirmation", MailSendConfirmationWorkflow(self.mail_send).build()
        )
