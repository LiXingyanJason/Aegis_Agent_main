"""有反馈的协调→专家子图→观察循环；不生成全量步骤或调度DAG。"""
from uuid import UUID, uuid5

from langgraph.graph import START, END, StateGraph
from pydantic import ValidationError

from aegis_agent_worker.agent.contracts.cooperation.decisions import CooperationDecision
from aegis_agent_worker.agent.contracts.cooperation.messages import DelegationMessage, ExpertResult
from aegis_agent_worker.agent.contracts.decision import AgentHandoff
from aegis_agent_worker.agent.routing.team_registry import TeamValidationError
from aegis_agent_worker.agent.specialists.cooperation_agent import CooperationOutputError
from aegis_agent_worker.agent.workflows.cooperation.context import expert_input, team_context
from aegis_agent_worker.agent.workflows.cooperation.state import CooperationState
from aegis_agent_worker.harness.context import get_execution
from aegis_agent_worker.harness.exceptions import HarnessError
from aegis_agent_worker.harness.failures import execution_error_code
from aegis_agent_worker.harness.policy_provider import DEFAULT_POLICIES
from aegis_agent_worker.llm.client import LLMError
from aegis_agent_worker.event.cooperation_context import association, cooperation_event_scope


class CooperationTeamWorkflow:
    """
      → coordinate（协调 Agent 决策）
      ├─ delegate → execute_expert → observe_expert ─┐
      │                                               └→ coordinate
      └─ finish → finish_team → END
    """
    def __init__(self, agent, registry, event_service, policy_provider=None):
        registry.require_ready()
        self._agent, self._registry, self._events = agent, registry, event_service
        self._policies = policy_provider or DEFAULT_POLICIES

    def build(self):
        graph = StateGraph(CooperationState)
        for name, method in (("prepare_team", self._prepare), ("coordinate", self._decide),
                             ("delegate", self._delegate), ("execute_expert", self._execute),
                             ("observe_expert", self._observe), ("finish_team", self._finish)):
            graph.add_node(name, method)
        graph.add_edge(START, "prepare_team")
        graph.add_conditional_edges("prepare_team", self._stopped,
                                    {True: END, False: "coordinate"})
        graph.add_conditional_edges("coordinate", self._route,
                                    {"stop": END, "delegate": "delegate", "finish": "finish_team"})
        graph.add_conditional_edges("delegate", self._stopped, {True: END, False: "execute_expert"})
        graph.add_conditional_edges("execute_expert", self._stopped,
                                    {True: END, False: "observe_expert"})
        graph.add_conditional_edges("observe_expert", self._stopped,
                                    {True: END, False: "coordinate"})
        graph.add_edge("finish_team", END)
        return graph.compile()

    def _runtime(self, state):
        runtime = get_execution()
        run = runtime.run
        expected = {"run_id": str(run.id), "tenant_id": str(run.tenant_id), "user_id": str(run.user_id),
                    "conversation_id": str(run.conversation_id) if run.conversation_id else None,
                    "run_type": run.run_type}
        if any(state.get(key) != value for key, value in expected.items()):
            raise HarnessError("COOPERATION_IDENTITY_MISMATCH", "协作任务身份不一致")
        profile = self._policies.current(state)
        if not profile.cooperation.enabled:
            raise HarnessError("COOPERATION_POLICY_DISABLED", "当前任务策略未开放团队协作")
        return run, profile

    async def _event(self, state, phase, delegation=None, error_code=None, result=None):
        run, profile = self._runtime(state)
        await self._events.record(run, phase=phase, round_no=state.get("cooperation_round", 0),
                                  policy_version=profile.version, delegation=delegation, error_code=error_code,result=result)

    async def _stop(self, state, code):
        await self._event(state, "unavailable", error_code=code)
        return {"cooperation_status": "unavailable", "cooperation_error_code": code,
                "cooperation_draft": {}, "cooperation_decision": {},
                "assistant_content": "协作任务尚未生成可确认的会议草稿，请明确要求或稍后重试。"}

    async def _prepare(self, state):
        _, profile = self._runtime(state)
        try:
            handoff = AgentHandoff.model_validate(state.get("handoff"))
            if (handoff.domain, handoff.intent, handoff.target) != (
                    "cooperation", "mail_calendar_meeting", None):
                raise ValueError("交接不匹配")
        except ValueError:
            return await self._stop(state, "COOPERATION_INVALID_HANDOFF")
        update = {"cooperation_goal": handoff.instruction, "cooperation_round": 0,
                  "cooperation_delegations": [], "cooperation_results": [],
                  "cooperation_repeated_results": 0, "cooperation_decision": {},
                  "cooperation_pending_result": {}, "cooperation_status": "running",
                  "cooperation_error_code": "", "cooperation_draft": {},
                  "execution_policy_version": profile.version}
        await self._event({**state, **update}, "started")
        return update

    async def _verify_history(self, state):
        run, _ = self._runtime(state)
        for raw_delegation, raw_result in zip(state["cooperation_delegations"],
                                              state["cooperation_results"], strict=True):
            await self._registry.verify(run, DelegationMessage.model_validate(raw_delegation),
                                        ExpertResult.model_validate(raw_result))

    async def _decide(self, state):
        _, profile = self._runtime(state)
        limits = profile.cooperation
        if state["cooperation_round"] >= limits.max_rounds:
            return await self._stop(state, "COOPERATION_ROUND_LIMIT")
        if state["cooperation_repeated_results"] >= limits.max_repeated_results:
            return await self._stop(state, "COOPERATION_NO_PROGRESS")
        try:
            # 暂停或再次委派后仍重新核验旧引用，不仅在首次结果返回时核验。
            await self._verify_history(state)
            context = team_context(state, limits)
            await self._event({**state,"cooperation_round":state["cooperation_round"]+1},"coordinating")
            decision = await self._agent.decide(context=context, participants=self._registry.participants(),
                remaining_delegations=max(0, limits.max_delegations-len(state["cooperation_delegations"])))
            decision = CooperationDecision.model_validate(decision)
            if decision.action == "delegate":
                self._registry.get(decision.expert)
                if len(state["cooperation_delegations"]) >= limits.max_delegations:
                    return await self._stop(state, "COOPERATION_DELEGATION_LIMIT")
        except TeamValidationError as error:
            return await self._stop(state, error.code)
        except (ValidationError, CooperationOutputError):
            return await self._stop(state, "COOPERATION_INVALID_DECISION")
        except (HarnessError, LLMError) as error:
            await self._event(state, "failed", error_code=execution_error_code(error))
            raise
        return {"cooperation_decision": decision.model_dump(mode="json"),
                "cooperation_round": state["cooperation_round"]+1}

    async def _delegate(self, state):
        run, _ = self._runtime(state)
        decision = CooperationDecision.model_validate(state["cooperation_decision"])
        self._registry.get(decision.expert)
        previous = state.get("cooperation_results", [])
        if previous and previous[-1]["expert"] == decision.expert and previous[-1]["status"] != "completed":
            # 无新的专家结果/用户输入，不能靠换delegation重置局部轮次并重复失败任务。
            return await self._stop(state, previous[-1].get("error_code") or "COOPERATION_NO_PROGRESS")
        try:
            await self._agent.check_delegation_capacity()
        except HarnessError as error:
            await self._event(state, "failed", error_code=error.code)
            raise
        sequence = len(state["cooperation_delegations"])+1
        delegation = DelegationMessage(delegation_id=uuid5(run.id, f"team-delegation:{sequence}"),
                                      sequence=sequence, target=decision.expert, goal=decision.goal)
        await self._event(state, "delegated", delegation)
        return {"cooperation_delegations": [*state["cooperation_delegations"],
                                             delegation.model_dump(mode="json")]}

    async def _execute(self, state):
        run, profile = self._runtime(state)
        delegation = DelegationMessage.model_validate(state["cooperation_delegations"][-1])
        binding = self._registry.get(delegation.target)
        try:
            await self._agent.check_delegation_capacity()
            # 包括嵌套interrupt恢复：先复核已共享来源，再继续原委派，不重做协调决策。
            await self._verify_history({**state, "cooperation_delegations": state["cooperation_delegations"][:-1]})
            # 由已编译子图继承父图Checkpoint；中断由LangGraph向父图传播，不在此重跑路由。
            await self._event(state,"expert_started",delegation)
            correlation=association(run.id,delegation.target,round=state["cooperation_round"],
                delegation_id=delegation.delegation_id,sequence=delegation.sequence)
            with cooperation_event_scope(run,correlation,self._events):
                output = await binding.graph.ainvoke(expert_input(
                    state, team_context(state, profile.cooperation), delegation))
            result = ExpertResult.model_validate(output["expert_result"])
            await self._registry.verify(run, delegation, result)
        except TeamValidationError as error:
            return await self._stop(state, error.code)
        except (ValidationError, KeyError):
            return await self._stop(state, "COOPERATION_INVALID_RESULT")
        except (HarnessError, LLMError) as error:
            await self._event(state, "failed", delegation, error_code=execution_error_code(error))
            raise
        # 只返回已核验的结果字段；不将专家整个State或原始响应写入父图。
        return {"cooperation_pending_result": result.model_dump(mode="json")}

    async def _observe(self, state):
        result = ExpertResult.model_validate(state["cooperation_pending_result"])
        previous = [ExpertResult.model_validate(item) for item in state["cooperation_results"]]
        signature = lambda item: (item.expert, item.status, item.references, item.draft,
                                  item.meeting_requirements, item.error_code, item.unresolved_fields)
        repeated = (state["cooperation_repeated_results"]+1
                    if previous and signature(previous[-1]) == signature(result) else 0)
        update = {"cooperation_results": [*state["cooperation_results"], result.model_dump(mode="json")],
                  "cooperation_pending_result": {}, "cooperation_repeated_results": repeated}
        try:
            _, profile = self._runtime(state)
            team_context({**state, **update}, profile.cooperation)
        except TeamValidationError as error:
            return await self._stop(state, error.code)
        await self._event(state, "expert_completed",
                          DelegationMessage.model_validate(state["cooperation_delegations"][-1]),result=result)
        return update

    async def _finish(self, state):
        decision = CooperationDecision.model_validate(state["cooperation_decision"])
        if decision.action == "finish_unavailable":
            latest = state["cooperation_results"][-1] if state["cooperation_results"] else {}
            return await self._stop(state, latest.get("error_code") or "COOPERATION_UNAVAILABLE")
        # 后续补问/重新委派的结果不能被旧草稿掩盖。finish必须以最新真实草稿结果收尾。
        latest = ExpertResult.model_validate(state["cooperation_results"][-1]) if state["cooperation_results"] else None
        if latest is None or latest.draft is None:
            return await self._stop(state, "COOPERATION_MISSING_DRAFT")
        try:
            await self._verify_history(state)
        except TeamValidationError as error:
            return await self._stop(state, error.code)
        await self._event(state, "draft_ready",result=latest)
        await self._event(state, "finished",result=latest)
        return {"cooperation_status": "draft_ready", "cooperation_draft": latest.draft.model_dump(mode="json"),
                "assistant_content": "会议草稿已生成，尚未写入日历，请核对并确认。"}

    @staticmethod
    def _stopped(state):
        return state.get("cooperation_status") == "unavailable"

    @classmethod
    def _route(cls, state):
        if cls._stopped(state):
            return "stop"
        return "delegate" if state["cooperation_decision"]["action"] == "delegate" else "finish"
