"""唯一协同入口：验证入口→规划→串行调度→验证结果→汇总。"""
from langgraph.graph import StateGraph,START,END
from aegis_agent_worker.agent.graph.state import TaskGraphState
from aegis_agent_worker.agent.contracts.decision import AgentHandoff
from aegis_agent_worker.agent.routing.cooperation_registry import CooperationPlanError
from aegis_agent_worker.harness.policy_provider import DEFAULT_POLICIES,COOPERATION_POLICY_VERSIONS
from .planning_workflow import CooperationPlanningWorkflow
from .runtime.scheduler import CooperationScheduler
from .runtime.progress import publish_progress


class CooperationWorkflow:
    def __init__(self,planner,service,registry_provider,policy_provider=None):
        self._planner,self._service,self._registry=planner,service,registry_provider
        self._policies=policy_provider or DEFAULT_POLICIES

    def build(self):
        graph=StateGraph(TaskGraphState)
        graph.add_node('validate_cooperation_entry',self._entry)
        graph.add_node('begin_cooperation',self._begin)
        graph.add_node('plan_cooperation',CooperationPlanningWorkflow(self._planner,self._registry,self._service).build())
        scheduler=CooperationScheduler(self._registry,self._service,self._policies)
        scheduler.register(graph,self._registry().graphs())
        graph.add_edge(START,'validate_cooperation_entry')
        graph.add_edge('validate_cooperation_entry','begin_cooperation_schedule')
        graph.add_edge('begin_cooperation_schedule','begin_cooperation')
        graph.add_edge('begin_cooperation','plan_cooperation')
        graph.add_conditional_edges('plan_cooperation',lambda state:'finish' if state.get('cooperation_error') else 'schedule',
            {'finish':END,'schedule':'validate_schedule'})
        return graph.compile()

    def _entry(self,state):
        """守卫入口
        当前任务绑定的 Harness 策略是否仍是允许执行的协同版本
        state['handoff'] 是否符合 AgentHandoff 结构，以及其中的 domain + intent 是否有已注册的协同任务配置。
        State 中是否已经出现调度器版本、计划绑定或步骤执行记录；若有，就报 cooperation_restart_blocked，防止从入口重新规划、重复执行步骤。"""
        if self._policies.current(state).version not in COOPERATION_POLICY_VERSIONS:
            raise CooperationPlanError('cooperation_policy_retired')
        handoff=AgentHandoff.model_validate(state['handoff'])
        self._registry().profile(handoff.domain,handoff.intent)
        if state.get('cooperation_scheduler_version') or state.get('cooperation_plan_binding') or state.get('cooperation_step_metrics'):
            raise CooperationPlanError('cooperation_restart_blocked')
        return {}

    async def _begin(self,state):
        await publish_progress(self._service,state,'started')
        return {}
