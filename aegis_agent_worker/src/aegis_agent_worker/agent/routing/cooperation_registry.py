"""静态协同能力注册与DAG验证：不调用工具/模型，不动态构造执行拓扑。"""
from dataclasses import dataclass
from types import MappingProxyType
from typing import get_args
from langgraph.graph.state import CompiledStateGraph
from aegis_agent_worker.agent.contracts.cooperation_plan import CooperationPlan
from aegis_agent_worker.agent.contracts.cooperation import TaskStepResult,snapshot_digest
from aegis_agent_worker.agent.routing.workflow_router import WorkflowRoutingError
from aegis_agent_worker.agent.contracts.cooperation_outputs import OUTPUT_CONTRACTS,OUTPUT_DOMAINS,OUTPUT_RISKS
from .cooperation_profiles import MAIL_CALENDAR_PROFILES


class CooperationPlanError(WorkflowRoutingError):
    def __init__(self,code):
        super().__init__(code)


@dataclass(frozen=True)
class CooperationCapability:
    workflow: str
    domain: str
    output_contract: str
    input_contracts: tuple[str,...]
    phase: str
    task_inputs: tuple[str,...]=()
    risk: str='read'
    optional_input_contracts: tuple[str,...]=()

    @property
    def capability_id(self):
        return f'{self.domain}.{self.phase}'

    @property
    def effect(self):
        return {'read':'read','draft_only':'internal_draft','approval_required':'approved_external_write'}[self.risk]

    def describe(self):
        return dict(capability=self.capability_id,workflow=self.workflow,domain=self.domain,expected_output=self.output_contract,
            phase=self.phase,input_contracts=list(self.input_contracts),optional_input_contracts=list(self.optional_input_contracts),
            task_inputs=list(self.task_inputs),risk=self.risk,effect=self.effect)


MEETING_STEPS=(
    CooperationCapability('cooperation_mail_requirements','mail','MailRequirementsV1',('user_request',),'mail_requirements',('user_request','verified_target')),
    CooperationCapability('cooperation_calendar_candidates','calendar','CalendarCandidatesV1',('MailRequirementsV1',),'calendar_candidates'),
    CooperationCapability('cooperation_meeting_draft','calendar','MeetingDraftV1',('MailRequirementsV1','CalendarCandidatesV1'),'meeting_draft',risk='approval_required'),
)


class CooperationRegistry:
    version='cooperation-catalog-v4'

    def __init__(self,capabilities,graphs,profiles=MAIL_CALENDAR_PROFILES,*,result_handlers=None):
        capabilities=tuple(capabilities)
        self._capabilities=MappingProxyType({item.workflow:item for item in capabilities})
        if len(self._capabilities)!=len(capabilities): raise ValueError('重复协同步骤注册')
        self._graphs=MappingProxyType(dict(graphs))
        self._result_handlers=MappingProxyType(dict(result_handlers or {}))
        self._registered=frozenset(self._graphs)
        profiles=tuple(profiles)
        self._profiles=MappingProxyType({(item.domain,item.intent):item for item in profiles})
        if len(self._profiles)!=len(profiles) or len({item.name for item in profiles})!=len(profiles):
            raise ValueError('重复协同任务规范注册')
        if set(self._capabilities)!=self._registered:
            raise ValueError('协同声明必须与内部实际步骤一一对应')
        for item in capabilities:
            if item.output_contract not in OUTPUT_CONTRACTS:
                raise ValueError('协同输出契约尚未注册为Schema')
            if not isinstance(self._graphs[item.workflow],CompiledStateGraph):
                raise ValueError('协同能力必须绑定内部编译子图')
            if item.risk!=OUTPUT_RISKS[item.output_contract] or not set(item.task_inputs)<={'user_request','verified_target'}:
                raise ValueError('无效步骤风险或任务输入声明')
            if (item.domain!=OUTPUT_DOMAINS[item.output_contract] or
                    get_args(OUTPUT_CONTRACTS[item.output_contract].model_fields['step_id'].annotation)!=(item.phase,)):
                raise ValueError('步骤领域与输出契约不匹配')
            if (set(item.input_contracts)&set(item.optional_input_contracts)
                    or len(set(item.input_contracts))!=len(item.input_contracts)
                    or not set(item.input_contracts)<=set(OUTPUT_CONTRACTS)|{'user_request'}
                    or ('user_request' in item.input_contracts and 'user_request' not in item.task_inputs)
                    or len(set(item.optional_input_contracts))!=len(item.optional_input_contracts)
                    or not set(item.optional_input_contracts)<=set(OUTPUT_CONTRACTS)):
                raise ValueError('可选输入必须是独立且已注册的证据契约')
        for profile in profiles:
            if not profile.available_capabilities<={item.capability_id for item in capabilities}:
                raise ValueError('任务包含未注册能力')
            outputs={item.output_contract for item in self.candidates(profile)}
            if profile.goal.output_contract not in outputs:
                raise ValueError('任务目标没有已注册生产者')
            if not set(profile.goal.required_evidence)<=outputs:
                raise ValueError('目标缺少风险范围内的证据能力')
        self.digest=snapshot_digest({'steps':self.catalog(),'profiles':[item.describe() for item in sorted(profiles,key=lambda p:p.name)]})

    def catalog(self,profile=None):
        return [item.describe() for item in self.candidates(profile)]

    def candidates(self,profile=None):
        return tuple(item for _,item in sorted(self._capabilities.items()) if profile is None or (
            item.capability_id in profile.available_capabilities and profile.risk_limit.permits(item.effect)))

    def unique_plan(self,*,domain,intent,has_verified_target,policy):
        from .cooperation_resolution import unique_plan
        return unique_plan(self,self.profile(domain,intent),has_verified_target=has_verified_target,policy=policy)

    def profile(self,domain,intent):
        try: return self._profiles[(domain,intent)]
        except KeyError: raise CooperationPlanError('plan_entry_mismatch') from None

    def graphs(self):
        return dict(self._graphs)

    def assert_executable(self):
        for item in self._capabilities.values():
            handler=self.result_handler(item.output_contract)
            if not callable(handler.verify) or (handler.present is not None and not callable(handler.present)):
                raise ValueError('步骤必须注册业务结果校验器')
            TaskStepResult(step_id=item.phase,status='unavailable',issue_code=handler.failure_issue)

    def result_handler(self,output_contract):
        try: return self._result_handlers[output_contract]
        except KeyError: raise CooperationPlanError('result_handler_missing') from None

    def capability(self, workflow):
        try: return self._capabilities[workflow]
        except KeyError: raise CooperationPlanError('unregistered_step_workflow') from None

    def validate(self,raw,*,has_verified_target,policy,domain,intent):
        """协同计划的执行前守卫：它检查模型给出的步骤计划是否符合已注册的能力和依赖规则。只有全部通过，才返回可供工作流使用的计划与绑定信息。"""
        def reject(code): raise CooperationPlanError(code)
        profile=self.profile(domain,intent)
        try: plan=CooperationPlan.model_validate(raw.model_dump(mode='json') if isinstance(raw,CooperationPlan) else raw)
        except ValueError: reject('invalid_plan_schema')
        data=plan.model_dump(mode='json')
        import json
        if len(json.dumps(data,ensure_ascii=False).encode())>policy.max_plan_bytes: reject('plan_bytes_exceeded')
        if len(plan.steps)>policy.max_steps: reject('step_limit_exceeded')
        by_id={item.step_id:item for item in plan.steps}
        if len(by_id)!=len(plan.steps): reject('duplicate_step_id')
        specs={}
        for step in plan.steps:
            spec=self._capabilities.get(step.workflow)
            if spec is None or step.workflow not in self._registered: reject('unregistered_step_workflow')
            if spec.capability_id not in profile.available_capabilities: reject('step_outside_profile')
            if not profile.risk_limit.permits(spec.effect): reject('step_risk_exceeded')
            specs[step.step_id]=spec
            if step.expected_output!=spec.output_contract: reject('output_contract_mismatch')
            if len(step.depends_on)>policy.max_dependencies: reject('dependency_limit_exceeded')
            if len(set(step.depends_on))!=len(step.depends_on): reject('duplicate_dependency')
            if step.step_id in step.depends_on or any(key not in by_id for key in step.depends_on): reject('invalid_dependency')
        if len({item.phase for item in specs.values()})!=len(specs): reject('duplicate_step_phase')
        # Kahn排序仅用于确定性校验/顺序记录，不是运行期任务调度器。
        order=[];remaining=set(by_id)
        while remaining:
            ready=sorted(key for key in remaining if set(by_id[key].depends_on)<=set(order))
            if not ready: reject('cyclic_dependency')
            order.extend(ready);remaining.difference_update(ready)
        depth={}
        for key in order:
            depth[key]=1+max((depth[parent] for parent in by_id[key].depends_on),default=0)
        if max(depth.values(),default=0)>policy.max_depth: reject('plan_depth_exceeded')
        for step in plan.steps:
            spec=specs[step.step_id];contracts=[];referenced=[]
            if 'verified_target' in spec.task_inputs and has_verified_target and not any(
                    reference.kind=='task' and reference.name=='verified_target' for reference in step.inputs):
                reject('target_reference_required')
            for reference in step.inputs:
                if reference.kind=='task':
                    if reference.name not in spec.task_inputs: reject('task_input_outside_entry')
                    if reference.name=='verified_target':
                        if not has_verified_target: reject('unverified_target_reference')
                    else: contracts.append(reference.name)
                else:
                    producer=by_id.get(reference.step_id)
                    if producer is None or reference.step_id not in step.depends_on: reject('invalid_step_reference')
                    if reference.output_contract!=producer.expected_output: reject('input_contract_mismatch')
                    contracts.append(reference.output_contract);referenced.append(reference.step_id)
            tokens=[reference.model_dump_json() for reference in step.inputs]
            if len(set(tokens))!=len(tokens): reject('duplicate_input')
            if (any(contracts.count(name)!=1 for name in spec.input_contracts)
                    or any(contracts.count(name)!=1 for name in set(contracts))
                    or not set(contracts)<=set(spec.input_contracts)|set(spec.optional_input_contracts)):
                reject('input_contract_mismatch')
            if set(referenced)!=set(step.depends_on): reject('dependency_input_mismatch')
        goals=[step for step in plan.steps if step.expected_output==profile.goal.output_contract]
        if len(goals)!=1: reject('required_goal_missing')
        # 所有步骤必须贡献给可信任务目标，不能夹带无关查询或提前结束任务。
        required=set()
        def include(key):
            if key in required: return
            required.add(key)
            for parent in by_id[key].depends_on: include(parent)
        include(goals[0].step_id)
        if required!=set(by_id): reject('unrelated_step')
        if not set(profile.goal.required_evidence)<={by_id[key].expected_output for key in required-{goals[0].step_id}}:
            reject('goal_evidence_missing')
        return plan,dict(catalog_version=self.version,catalog_digest=self.digest,plan_digest=snapshot_digest(data),
            execution_profile=profile.name,policy_version=policy.version,step_order=order,
            goal_digest=snapshot_digest(profile.goal.describe()),risk_limit=profile.risk_limit.describe(),
            phase_steps={specs[key].phase:key for key in order})
