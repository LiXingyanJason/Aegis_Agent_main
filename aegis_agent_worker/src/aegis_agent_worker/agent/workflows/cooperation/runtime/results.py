"""步骤结果的有界暂存、读取及真实等待状态；不识别具体领域。"""
from aegis_agent_worker.agent.contracts.cooperation import TaskStepResult
from .plan_state import update_plan_progress
from .progress import publish_progress
from .run_context import run_from


def stage_result(state,result,registry_provider):
    progress=update_plan_progress(state,result,registry_provider)
    return {'cooperation_pending_result':result.model_dump(mode='json'),
        **(progress if result.status=='waiting_confirmation' else {})}


async def record_waiting(service,state,result):
    if result.status!='waiting_confirmation': raise ValueError('仅允许发布真实草稿等待状态')
    await service.record_waiting(run_from(state),result)
    key=state['cooperation_plan_binding']['phase_steps'][result.step_id]
    await publish_progress(service,state,'waiting_confirmation','waiting_confirmation',phase=result.step_id,step_id=key)


def require_succeeded_result(state,phase):
    result=TaskStepResult.model_validate(state['cooperation_results'][phase])
    if result.step_id!=phase or result.status!='succeeded':
        raise ValueError('无有效的已完成步骤结果')
    return result
