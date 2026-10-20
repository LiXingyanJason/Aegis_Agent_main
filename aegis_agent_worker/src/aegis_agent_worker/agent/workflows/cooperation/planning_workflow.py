"""协同入口自己的规划子图；主路由不感知步骤、规划结果或失败细节。"""
from time import monotonic
from langgraph.graph import StateGraph, START, END
from aegis_agent_worker.agent.contracts.decision import AgentHandoff
from aegis_agent_worker.agent.graph.state import TaskGraphState
from aegis_agent_worker.agent.routing.cooperation_registry import CooperationPlanError
from aegis_agent_worker.agent.specialists.cooperation_planner import CooperationPlanOutputError
from .runtime.plan_state import verify_plan_state,require_engine
from aegis_agent_worker.harness.policy_provider import DEFAULT_POLICIES
from .runtime.progress import publish_progress
from .runtime.run_context import run_from


class CooperationPlanningWorkflow:
    """可复用规划、注册表验证和安全审计；不访问业务工具、不负责步骤调度。"""
    def __init__(self, planner, registry_provider, audit_service, policy_provider=None):
        self._planner = planner
        self._registry = registry_provider
        self._audit_service = audit_service
        self._policies = policy_provider or DEFAULT_POLICIES

    def build(self):
        graph = StateGraph(TaskGraphState)
        graph.add_node('generate_and_validate_plan', self._plan)
        graph.add_node('record_plan_audit', self._audit)
        graph.add_conditional_edges(START, self._entry, {'reuse': END, 'plan': 'generate_and_validate_plan'})
        graph.add_edge('generate_and_validate_plan', 'record_plan_audit')
        graph.add_edge('record_plan_audit', END)
        return graph.compile()

    def _entry(self, state):
        require_engine(state)
        if state.get('cooperation_plan') or state.get('cooperation_plan_binding'):
            # 最终引擎内部重入仍须复核；不兼容开发期目录或引擎。
            verify_plan_state(state, self._registry)
            return 'reuse'
        return 'plan'

    async def _plan(self, state):
        await publish_progress(self._audit_service,state,'planning')
        started = monotonic()
        registry = self._registry()
        policy = self._policies.current(state).cooperation
        audit = {'catalog_version': registry.version, 'catalog_digest': registry.digest,
                 'policy_version': policy.version}
        repair_count = 0
        try:
            handoff = AgentHandoff.model_validate(state['handoff'])
            profile = registry.profile(handoff.domain,handoff.intent)
            plan=registry.unique_plan(domain=handoff.domain,intent=handoff.intent,
                has_verified_target=handoff.target is not None,policy=policy)
            audit['planning_strategy']='deterministic' if plan is not None else 'agent'
            if plan is None:
                plan, repair_count = await self._planner.plan(message=state.get('latest_user_message', ''),
                    handoff=handoff, catalog=registry.catalog(profile), task_profile=profile.describe())
            plan, binding = registry.validate(plan, has_verified_target=handoff.target is not None, policy=policy,
                domain=handoff.domain,intent=handoff.intent)
        except (CooperationPlanError, CooperationPlanOutputError, ValueError) as error:
            code = error.code if isinstance(error, CooperationPlanError) else 'invalid_plan_schema'
            question = '协同计划未通过步骤、依赖或来源校验，请明确邮件对象和会议目标后重试。'
            return {'cooperation_error': code, 'cooperation_clarification': {'code': code, 'question': question},
                'assistant_content': question, 'cooperation_plan': {}, 'cooperation_plan_binding': {},
                'cooperation_progress': {}, 'cooperation_current_step': '', 'cooperation_status': 'unavailable',
                'cooperation_plan_audit': {**audit, 'outcome': code,
                    'repair_count': getattr(error, 'repair_count', repair_count),
                    'duration_ms': round((monotonic()-started)*1000)}}
        except Exception:
            # 网络/预算错误仍按原异常分类失败，不变成格式修复或默认工具执行。
            await self._audit_service.record_plan(run_from(state), {**audit, 'outcome': 'model_failed',
                'duration_ms': round((monotonic()-started)*1000)})
            await publish_progress(self._audit_service,state,'failed','failed')
            raise
        return {'cooperation_plan': plan.model_dump(mode='json'), 'cooperation_plan_binding': binding,
            'cooperation_progress': {key: {'status': 'pending', 'result_ref': None} for key in binding['step_order']},
            'cooperation_current_step': binding['step_order'][0], 'cooperation_status': 'planned',
            'cooperation_error': '', 'cooperation_clarification': {},
            'cooperation_plan_audit': {**audit, 'outcome': 'validated', 'repair_count': repair_count,
                'step_count': len(plan.steps), 'plan_digest': binding['plan_digest'],
                'duration_ms': round((monotonic()-started)*1000)}}

    async def _audit(self, state):
        await self._audit_service.record_plan(run_from(state), state['cooperation_plan_audit'])
        if state.get('cooperation_error'):
            await publish_progress(self._audit_service,state,'finished','unavailable')
        return {}
