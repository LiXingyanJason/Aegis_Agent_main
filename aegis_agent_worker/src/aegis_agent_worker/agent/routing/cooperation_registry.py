"""静态协同能力注册与DAG验证：不调用工具/模型，不动态构造执行拓扑。"""
from dataclasses import dataclass
from types import MappingProxyType
from aegis_agent_worker.agent.contracts.cooperation_plan import CooperationPlan
from aegis_agent_worker.agent.contracts.cooperation import snapshot_digest
from aegis_agent_worker.agent.routing.workflow_router import WorkflowRoutingError
from aegis_agent_worker.agent.contracts.cooperation_outputs import OUTPUT_CONTRACTS


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
    entry_workflow: str='mail_calendar_meeting'

    def describe(self):
        return dict(workflow=self.workflow,domain=self.domain,expected_output=self.output_contract,
            input_contracts=list(self.input_contracts),risk='draft_only' if self.phase=='meeting_draft' else 'read')


FIXED_MEETING_STEPS=(
    CooperationCapability('cooperation_mail_requirements','mail','MailRequirementsV1',('user_request',),'mail_requirements'),
    CooperationCapability('cooperation_calendar_candidates','calendar','CalendarCandidatesV1',('MailRequirementsV1',),'calendar_candidates'),
    CooperationCapability('cooperation_meeting_draft','calendar','MeetingDraftV1',('MailRequirementsV1','CalendarCandidatesV1'),'meeting_draft'),
)


class CooperationRegistry:
    version='cooperation-catalog-v1'

    def __init__(self,capabilities,registered_workflows,conversation_capabilities):
        capabilities=tuple(capabilities)
        self._capabilities=MappingProxyType({item.workflow:item for item in capabilities})
        if len(self._capabilities)!=len(capabilities): raise ValueError('重复协同步骤注册')
        self._registered=frozenset(registered_workflows)
        entries={(item.domain,item.intent,item.workflow) for item in conversation_capabilities}
        for item in capabilities:
            if item.output_contract not in OUTPUT_CONTRACTS:
                raise ValueError('协同输出契约尚未注册为Schema')
            if (item.workflow not in self._registered or item.entry_workflow not in self._registered
                    or ('mail','arrange_meeting_from_mail',item.entry_workflow) not in entries):
                raise ValueError('协同能力未绑定实际根图节点或入口能力')
            if item.domain!=('mail' if item.output_contract=='MailRequirementsV1' else 'calendar'):
                raise ValueError('步骤领域与输出契约不匹配')
        self.digest=snapshot_digest(self.catalog())

    def catalog(self):
        return [item.describe() for _,item in sorted(self._capabilities.items())]

    @property
    def enabled(self): return bool(self._capabilities)

    def validate(self,raw,*,has_verified_target,policy):
        """协同计划的执行前守卫：它检查模型给出的步骤计划是否符合已注册的能力和依赖规则。只有全部通过，才返回可供工作流使用的计划与绑定信息。"""
        def reject(code): raise CooperationPlanError(code)
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
            specs[step.step_id]=spec
            if step.expected_output!=spec.output_contract: reject('output_contract_mismatch')
            if len(step.depends_on)>policy.max_dependencies: reject('dependency_limit_exceeded')
            if len(set(step.depends_on))!=len(step.depends_on): reject('duplicate_dependency')
            if step.step_id in step.depends_on or any(key not in by_id for key in step.depends_on): reject('invalid_dependency')
        # Kahn排序仅用于确定性校验/顺序记录，不是运行期任务调度器。
        order=[];remaining=set(by_id)
        while remaining:
            ready=sorted(key for key in remaining if set(by_id[key].depends_on)<=set(order))
            if not ready: reject('cyclic_dependency')
            order.extend(ready);remaining.difference_update(ready)
        for step in plan.steps:
            spec=specs[step.step_id];contracts=[];referenced=[]
            if spec.phase=='mail_requirements' and has_verified_target and not any(
                    reference.kind=='task' and reference.name=='verified_target' for reference in step.inputs):
                reject('target_reference_required')
            for reference in step.inputs:
                if reference.kind=='task':
                    if spec.phase!='mail_requirements': reject('task_input_outside_entry')
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
            if sorted(contracts)!=sorted(spec.input_contracts): reject('input_contract_mismatch')
            if set(referenced)!=set(step.depends_on): reject('dependency_input_mismatch')
        expected=tuple(item.workflow for item in FIXED_MEETING_STEPS)
        if tuple(by_id[key].workflow for key in order)!=expected: reject('unsupported_execution_profile')
        return plan,dict(catalog_version=self.version,catalog_digest=self.digest,plan_digest=snapshot_digest(data),
            execution_profile='mail-calendar-meeting-v1',policy_version=policy.version,step_order=order,
            phase_steps={specs[key].phase:key for key in order})
