"""单节点的稳定失败转换；不重试、不捕获LangGraph中断或进程取消。"""
from aegis_agent_worker.agent.contracts.cooperation import TaskStepResult
from aegis_agent_worker.harness.exceptions import HarnessError
from aegis_agent_worker.llm.client import LLMError
from aegis_agent_worker.tool.contracts import ToolError
from aegis_agent_worker.harness.cooperation_scope import cooperation_step_scope
from .plan_state import verify_step_inputs,verify_plan_state
from .results import stage_result
from .run_context import run_from


async def execution_boundary(service,state):
    await service.check_execution(run_from(state))


def guarded_step(method, phase, registry_provider):
    async def execute(state):
        try:
            verify_step_inputs(state,registry_provider,phase)
            with cooperation_step_scope(state['cooperation_current_step'],state['cooperation_plan_binding']['plan_digest']):
                return await method(state)
        except (HarnessError, LLMError, ToolError, ValueError, TimeoutError) as error:
            if isinstance(error,HarnessError) and error.code=='EXECUTION_CANCELLED':
                raise
            plan,binding=verify_plan_state(state,registry_provider)
            step=next(item for item in plan.steps if item.step_id==binding['phase_steps'][phase])
            handler=registry_provider().result_handler(step.expected_output)
            issue=handler.failure_issue
            code=getattr(error,'code',None) or ('LLM_UNAVAILABLE' if isinstance(error,LLMError) else
                'EXECUTION_TIMEOUT' if isinstance(error,TimeoutError) else 'STEP_VALIDATION_FAILED')
            return {'cooperation_failure_code':code,'cooperation_error':issue,
                **dict(handler.failure_updates),
                'assistant_content':handler.failure_content,
                **stage_result(state,TaskStepResult(step_id=phase,status='unavailable',issue_code=issue),registry_provider)}
    return execute
