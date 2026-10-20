"""路径唯一时从契约构造确定计划；有替代生产者/可选证据时交给模型。"""
from aegis_agent_worker.agent.contracts.cooperation_plan import CooperationPlan


class _RequiresPlanning(Exception):
    pass


def unique_plan(registry,profile,*,has_verified_target,policy):
    by_output={}
    for capability in registry.candidates(profile):
        by_output.setdefault(capability.output_contract,[]).append(capability)
    selected={}
    visiting=set()

    def resolve(output,depth=1):
        if output in visiting: raise _RequiresPlanning
        if output in selected: return selected[output]['step_id']
        if depth>policy.max_depth or len(selected)+len(visiting)>=policy.max_steps:
            from .cooperation_registry import CooperationPlanError
            raise CooperationPlanError('direct_plan_limit_exceeded')
        options=by_output.get(output,[])
        if len(options)!=1: raise _RequiresPlanning
        spec=options[0]
        if any(by_output.get(contract) for contract in spec.optional_input_contracts):
            raise _RequiresPlanning
        visiting.add(output)
        inputs=[]
        dependencies=[]
        for contract in spec.input_contracts:
            if contract=='user_request':
                inputs.append(dict(kind='task',name=contract))
            else:
                parent=resolve(contract,depth+1)
                dependencies.append(parent)
                inputs.append(dict(kind='step',step_id=parent,output_contract=contract))
        if has_verified_target and 'verified_target' in spec.task_inputs:
            inputs.append(dict(kind='task',name='verified_target'))
        selected[output]=dict(step_id=spec.phase,workflow=spec.workflow,depends_on=dependencies,
            inputs=inputs,expected_output=output)
        visiting.remove(output)
        return spec.phase

    try:
        resolve(profile.goal.output_contract)
    except _RequiresPlanning:
        return None
    # 构造不代表授权；调用方仍执行相同Registry.validate和Harness/Gateway守卫。
    return CooperationPlan.model_validate(dict(version=1,steps=list(selected.values())))
