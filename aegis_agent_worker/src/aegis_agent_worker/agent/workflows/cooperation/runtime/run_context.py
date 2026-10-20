"""从根图可信身份恢复协同任务归属；不读取业务正文。"""
from uuid import UUID
from aegis_agent_worker.repository.run_repository import ClaimedRun


def run_from(state):
    return ClaimedRun(id=UUID(state['run_id']),tenant_id=UUID(state['tenant_id']),
        user_id=UUID(state['user_id']),conversation_id=UUID(state['conversation_id']))
