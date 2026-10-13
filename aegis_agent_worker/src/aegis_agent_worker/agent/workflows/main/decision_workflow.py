"""主决策子图：模型推理、能力校验、目标核验、结构化交接和审计。"""

from time import monotonic
from typing import Any
import re
from uuid import UUID

from langgraph.graph import START, END, StateGraph

from aegis_agent_worker.agent.contracts.decision import (
    AgentHandoff, MainDecision, ResourceReference, clarify,
)
from aegis_agent_worker.agent.graph.state import TaskGraphState
from aegis_agent_worker.agent.routing.capability_selector import CapabilitySelector, capability_id
from aegis_agent_worker.agent.specialists.main_agent import MainDecisionOutputError
from aegis_agent_worker.observability.telemetry import get_tracer
from aegis_agent_worker.repository.run_repository import ClaimedRun


class MainDecisionState(TaskGraphState, total=False):
    """子图内部字段，不把候选列表和审计中间量暴露为根图协议。"""
    decision_references: list[dict[str, Any]]
    decision_outcome: str
    decision_duration_ms: int
    candidate_capabilities: list[str]
    filter_reason_codes: list[str]
    route_policy_version: str
    repair_count: int
    validation_issue_codes: list[dict[str, str]]


class MainDecisionWorkflow:
    """LLM 与下一步决策在图层；数据库访问与业务核验委托 Service。"""

    def __init__(self, main_agent, context_service, capabilities, capability_selector=None):
        self._main_agent = main_agent
        self._context_service = context_service
        self._capabilities = tuple(capabilities)
        self._selector = capability_selector or CapabilitySelector()

    def build(self):
        graph = StateGraph(MainDecisionState)
        graph.add_node("load_decision_context", self._load_context)
        graph.add_node("select_candidate_capabilities", self._select_candidates)
        graph.add_node("infer_main_decision", self._infer)
        graph.add_node("validate_decision_capability", self._validate_capability)
        graph.add_node("validate_decision_target", self._validate_target)
        graph.add_node("assemble_agent_handoff", self._assemble_handoff)
        graph.add_node("record_decision_audit", self._audit)
        graph.add_edge(START, "load_decision_context")
        graph.add_edge("load_decision_context", "select_candidate_capabilities")
        graph.add_edge("select_candidate_capabilities", "infer_main_decision")
        graph.add_edge("infer_main_decision", "validate_decision_capability")
        graph.add_conditional_edges(
            "validate_decision_capability", self._route_target,
            {"validate_target": "validate_decision_target", "assemble": "assemble_agent_handoff"},
        )
        graph.add_edge("validate_decision_target", "assemble_agent_handoff")
        graph.add_edge("assemble_agent_handoff", "record_decision_audit")
        graph.add_edge("record_decision_audit", END)
        return graph.compile()

    async def _load_context(self, state):
        """只为需要业务对象的已注册能力读取候选引用。"""
        references = []
        if any(item.domain == "mail" or item.requires_target for item in self._capabilities):
            references = await self._context_service.load_references(_run(state))
        return {"decision_references": references}

    def _select_candidates(self, state):
        """只存目录标识，最终候选再次与可信注册目录求交集。"""
        selected = self._selector.select(
            self._capabilities, state.get("latest_user_message", ""), state["decision_references"],
        )
        requested = set(selected.candidate_ids)
        candidates = [capability_id(item) for item in self._capabilities if capability_id(item) in requested]
        reasons = list(selected.reason_codes)
        if not candidates and self._capabilities:
            # 软规则不可导致能力消失；意外空筛选恢复目录，绝不直接执行工具。
            candidates = [capability_id(item) for item in self._capabilities]
            reasons.append("empty_selection_restore_catalog")
        return {
            "candidate_capabilities": candidates, "filter_reason_codes": reasons,
            "route_policy_version": self._selector.policy_version,
        }

    async def _infer(self, state):
        """
        主 Agent 调用模型；非法结构输出改为澄清，模型故障由任务生命周期收尾。性能优化层
        两层 Capability 决策处理：
            - CapabilitySelector：用保守规则(字符串匹配)筛选“哪些能力值得交给 LLM 考虑”，不确定时保留全部，不选择最终工作流。
            - 主 Agent MainDecisionWorkflow：由 LLM 判断最终领域、意图，或者选择澄清／普通回复。
        """
        started = monotonic()
        outcome = "validated"
        if not state["candidate_capabilities"]:
            return {
                "main_decision": clarify("当前没有可用的领域能力，请稍后重试或联系管理员。")
                    .model_dump(mode="json"),
                "decision_outcome": "empty_catalog", "decision_duration_ms": 0,
            }
        ordinal = _mail_ordinal(state.get("latest_user_message", ""))
        if ordinal is not None and (ordinal < 1 or not any(
            item.get("resource_type") == "email" and item.get("position") == ordinal
            for item in state["decision_references"]
        )):
            return {**self._clarification("最近邮件卡片中没有有效的对应序号，请重新查询并选择邮件。",
                                         "invalid_mail_ordinal"), "decision_duration_ms": 0}
        with get_tracer(__name__).start_as_current_span("agent.main.decide") as span:
            span.set_attribute("aegis.run_id", state["run_id"])
            span.set_attribute("gen_ai.provider.name", getattr(self._main_agent, "provider_name", "unknown"))
            span.set_attribute("gen_ai.request.model", getattr(self._main_agent, "model_name", "unknown"))
            try:
                decision = await self._main_agent.decide(
                    message=state.get("latest_user_message", ""), history=state.get("history", []),
                    capabilities=[item.model_dump(mode="json") for item in self._capabilities
                                  if capability_id(item) in state["candidate_capabilities"]],
                    references=state["decision_references"],
                )
                if decision.decision == "delegate" and decision.domain == "mail" and decision.intent != "query_mail":
                    # 代码保证自然语言只导航卡片，不能触发摘要或起草。
                    decision = MainDecision(
                        decision="delegate", domain="mail", intent="query_mail",
                        instruction=decision.instruction, target=decision.target,
                    )
            except MainDecisionOutputError:
                outcome = "invalid_output"
                decision = clarify("主决策结果未通过校验，请重新说明要查询日历、安排会议还是查看邮件。")
            except ValueError:
                outcome = "invalid_output"
                decision = clarify("暂时无法确定要执行的任务，请说明要查询日历、安排会议，还是处理哪一封邮件。")
            except Exception:
                # 记录模型不可用且不产生工具调用；异常由现有生命周期服务分类处理。
                metadata = getattr(self._main_agent, "output_metadata", {})
                await self._record({**state, **metadata}, None, "model_failed", round((monotonic() - started) * 1000))
                raise
        return {
            "main_decision": decision.model_dump(mode="json"), "decision_outcome": outcome,
            "decision_duration_ms": round((monotonic() - started) * 1000),
            **getattr(self._main_agent, "output_metadata", {"repair_count": 0, "validation_issue_codes": []}),
        }

    def _capability(self, decision):
        return next((item for item in self._capabilities
                     if item.domain == decision.domain and item.intent == decision.intent), None)

    def _validate_capability(self, state):
        """模型只允许选择当前领域工厂显式声明的能力。"""
        decision = MainDecision.model_validate(state["main_decision"])
        if decision.decision == "delegate":
            ordinal = _mail_ordinal(state.get("latest_user_message", ""))
            if ordinal is not None and decision.domain == "mail" and decision.target is None:
                return self._clarification("请从最近邮件卡片中选择对应邮件，不能用邮箱列表代替序号目标。",
                                           "invalid_mail_ordinal")
            capability = self._capability(decision)
            if capability is None:
                return self._clarification("当前尚不支持这项操作，请选择已有日历或邮件功能。",
                                           "unregistered_capability")
            if capability_id(capability) not in state["candidate_capabilities"]:
                return self._clarification("执行方向与当前任务不一致，请明确要处理日历还是邮件。",
                                           "outside_candidate_set")
            if capability.requires_target and decision.target is None:
                return self._clarification("请明确选择要处理的邮件。", "missing_target")
        return {}

    @staticmethod
    def _route_target(state):
        decision = MainDecision.model_validate(state["main_decision"])
        return "validate_target" if decision.decision == "delegate" and decision.target else "assemble"

    async def _validate_target(self, state):
        """候选范围由图检查；归属和版本等业务事实由 Service 核验。"""
        decision = MainDecision.model_validate(state["main_decision"])
        offered = next((item for item in state["decision_references"]
                        if item["id"] == str(decision.target.id)
                        and item["resource_type"] == decision.target.resource_type), None)
        resolved = None
        ordinal = _mail_ordinal(state.get("latest_user_message", ""))
        if ordinal is not None and (offered is None or offered.get("position") != ordinal):
            return self._clarification("该邮件不对应最近卡片中的指定序号，请重新选择。", "invalid_mail_ordinal")
        if offered is not None:
            resolved = await self._context_service.validate_reference(_run(state), decision.target)
        if resolved is None:
            return self._clarification("目标邮件不存在、已更新或无法确定，请重新选择要处理的邮件。",
                                       "invalid_target")
        decision = decision.model_copy(update={"target": ResourceReference.model_validate(resolved)})
        return {"main_decision": decision.model_dump(mode="json")}

    def _assemble_handoff(self, state):
        """构造专家输入和根图条件边输出，未通过校验的决策不能形成交接。"""
        decision = MainDecision.model_validate(state["main_decision"])
        handoff = None
        workflow = "clarify" if decision.decision == "clarify" else "general_reply"
        if decision.decision == "delegate":
            capability = self._capability(decision)
            if capability is None or capability_id(capability) not in state["candidate_capabilities"]:
                raise RuntimeError("未校验的能力不能交接")
            handoff = AgentHandoff(domain=decision.domain, intent=decision.intent,
                                   instruction=decision.instruction, target=decision.target)
            workflow = capability.workflow
        return {
            "handoff": handoff.model_dump(mode="json") if handoff else {},
            "intent": decision.intent or decision.decision, "workflow": workflow,
        }

    async def _audit(self, state):
        await self._record(state, MainDecision.model_validate(state["main_decision"]),
                           state["decision_outcome"], state["decision_duration_ms"])
        return {}

    async def _record(self, state, decision, outcome, duration_ms):
        """图层提供审计内容，Service 执行持久化与事件发布。"""
        await self._context_service.record_decision(
            _run(state), decision=decision, outcome=outcome, duration_ms=duration_ms,
            capabilities=self._capabilities,
            model_provider=getattr(self._main_agent, "provider_name", "unknown"),
            model_name=getattr(self._main_agent, "model_name", "unknown"),
            candidate_capabilities=state.get("candidate_capabilities", []),
            filter_reason_codes=state.get("filter_reason_codes", []),
            route_policy_version=state.get("route_policy_version", self._selector.policy_version),
            repair_count=state.get("repair_count", 0),
            validation_issue_codes=state.get("validation_issue_codes", []),
        )

    @staticmethod
    def _clarification(question, outcome):
        return {"main_decision": clarify(question).model_dump(mode="json"), "decision_outcome": outcome}


def _run(state):
    """从可信根图状态还原当前会话任务归属。"""
    return ClaimedRun(id=UUID(state["run_id"]), tenant_id=UUID(state["tenant_id"]),
                      user_id=UUID(state["user_id"]), conversation_id=UUID(state["conversation_id"]))


def _mail_ordinal(message):
    """序号仅对最近卡片有效；多个序号先澄清，不按收件箱顺序猜测。"""
    # 完整主题中可能包含“第一封”，不能把标题文字当成卡片指代。
    text = re.sub(r'"[^"\n]*"|“[^”\n]*”|《[^》\n]*》', "", message)
    subject = re.search(r"(?:主题|标题)(?:为|是|叫|[:：])", text)
    if subject:
        text = text[:subject.start()]
    values = re.findall(r"第([一二三四五六七八九十百零两\d]+)封", text)
    if not values:
        return None
    numbers = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
               "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
    def parse(value):
        if value.isdecimal():
            return int(value) if len(value) <= 3 else -1
        if value in numbers:
            return numbers[value]
        if value.startswith("十") and value[1:] in numbers:
            return 10 + numbers[value[1:]]
        if value == "二十":
            return 20
        return -1
    parsed = {parse(value) for value in values}
    return next(iter(parsed)) if len(parsed) == 1 else -1
