"""邮件步骤的来源与计划事实复核；不承担通用调度。"""
from aegis_agent_worker.agent.contracts.cooperation import TaskStepInput
from aegis_agent_worker.agent.routing.cooperation_registry import CooperationPlanError
from ...runtime.result_handler import StepResultHandler
from ...runtime.run_context import run_from


async def verify_mail(service,state,result):
    expected=TaskStepInput.model_validate(state['cooperation_input']).source
    if result.source!=expected or result.calendar_decision.model_dump(mode='json')!=state.get('calendar_decision'):
        raise CooperationPlanError('step_source_mismatch')
    await service.verify_source(run_from(state),result.source,result.source_digest)


MAIL_RESULT_HANDLERS={
    'MailRequirementsV1':StepResultHandler(verify_mail,'tool_unavailable',
        failure_content='协同步骤未完成，已停止后续执行；不会自动重试或重新写入日历。'),
}
