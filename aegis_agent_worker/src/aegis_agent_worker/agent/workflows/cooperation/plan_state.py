"""协同Checkpoint绑定及有界进度；不调度、不访问业务源。"""
import json
from aegis_agent_worker.agent.contracts.decision import AgentHandoff
from aegis_agent_worker.agent.routing.cooperation_registry import CooperationPlanError
from aegis_agent_worker.harness.policy_provider import DEFAULT_POLICIES
from aegis_agent_worker.agent.contracts.cooperation_outputs import OUTPUT_CONTRACTS


def verify_plan_state(state,registry_provider,*,required=False):
    if not state.get('cooperation_plan') and not state.get('cooperation_plan_binding'):
        if required: raise CooperationPlanError('missing_validated_plan')
        return None
    if registry_provider is None: raise CooperationPlanError('plan_registry_unavailable')
    registry=registry_provider()
    handoff=AgentHandoff.model_validate(state['handoff'])
    if handoff.domain!='mail' or handoff.intent!='arrange_meeting_from_mail':
        raise CooperationPlanError('plan_entry_mismatch')
    plan,binding=registry.validate(state.get('cooperation_plan'),has_verified_target=handoff.target is not None,
        policy=DEFAULT_POLICIES.current(state).cooperation)
    if binding!=state.get('cooperation_plan_binding'):
        raise CooperationPlanError('plan_binding_changed')
    return plan,binding


def update_plan_progress(state,result,registry_provider):
    validated=verify_plan_state(state,registry_provider)
    if validated is None: return {}
    plan,binding=validated
    limits=DEFAULT_POLICIES.current(state).cooperation
    key=binding['phase_steps'][result.step_id]
    step=next(item for item in plan.steps if item.step_id==key)
    try:
        result=OUTPUT_CONTRACTS[step.expected_output].model_validate(result.model_dump(mode='json'))
    except ValueError:
        raise CooperationPlanError('step_result_contract_mismatch') from None
    data=result.model_dump(mode='json')
    results={**state.get('cooperation_results',{}),result.step_id:data}
    if len(json.dumps(data,ensure_ascii=False).encode())>limits.max_step_result_bytes:
        raise CooperationPlanError('step_result_bytes_exceeded')
    if len(json.dumps(results,ensure_ascii=False).encode())>limits.max_results_bytes:
        raise CooperationPlanError('results_bytes_exceeded')
    progress={step_id:dict(state.get('cooperation_progress',{}).get(step_id,{'status':'pending','result_ref':None}))
        for step_id in binding['step_order']}
    if result.status!='unavailable' and any(progress[dependency]['status']!='succeeded' for dependency in step.depends_on):
        raise CooperationPlanError('dependency_result_not_ready')
    progress[key]={'status':result.status,'result_ref':'cooperation_results.'+result.step_id}
    pending=next((item for item in binding['step_order'] if progress[item]['status'] not in {'succeeded','completed'}),'')
    status=result.status if result.status in {'unavailable','waiting_confirmation','completed','rejected'} else 'in_progress'
    return {'cooperation_progress':progress,'cooperation_current_step':pending,'cooperation_status':status}


def verify_step_inputs(state,registry_provider,phase):
    """消费交接前核验已完成依赖；不是choose_next，也不把模型状态当授权。"""
    validated=verify_plan_state(state,registry_provider)
    if validated is None: return
    plan,binding=validated
    key=binding['phase_steps'][phase]
    step=next(item for item in plan.steps if item.step_id==key)
    progress=state.get('cooperation_progress',{})
    if any(progress.get(dependency,{}).get('status')!='succeeded' for dependency in step.depends_on):
        raise CooperationPlanError('dependency_result_not_ready')
