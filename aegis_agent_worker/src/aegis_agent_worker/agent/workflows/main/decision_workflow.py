"""主决策子图：模型推理、能力校验、目标核验、结构化交接和审计。"""

from time import monotonic
from typing import Any
import json
from uuid import UUID

from langgraph.graph import START, END, StateGraph

from aegis_agent_worker.agent.contracts.decision import (
    AgentHandoff, MainDecision, ResourceReference, clarify,
)
from aegis_agent_worker.agent.graph.state import TaskGraphState
from aegis_agent_worker.agent.routing.capability_selector import CapabilitySelector, capability_id
from aegis_agent_worker.agent.routing.workflow_router import WorkflowRouter, WorkflowRoutingError
from aegis_agent_worker.agent.specialists.main_agent import MainDecisionOutputError
from aegis_agent_worker.agent.contracts.context import MainContextReadRequest, CONTEXT_SCHEMAS
from aegis_agent_worker.tool.contracts import ToolError
from aegis_agent_worker.harness.policy_provider import DEFAULT_POLICIES
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
    context_results: list[dict[str, Any]]
    context_request: dict[str, Any]
    context_round: int
    context_read_count: int
    context_error: bool


class MainDecisionWorkflow:
    """LLM 与下一步决策在图层；数据库访问与业务核验委托 Service。"""

    def __init__(self, main_agent, context_service, capabilities, capability_selector=None, context_tools=None, policy_provider=None, workflow_router=None):
        self._main_agent = main_agent
        self._context_service = context_service
        self._capabilities = tuple(capabilities)
        # 生产由根图传入实际注册节点；独立子图测试可使用能力声明目录。
        self._router = workflow_router or WorkflowRouter(
            self._capabilities, {item.workflow for item in self._capabilities})
        self._selector = capability_selector or CapabilitySelector()
        self._context_tools = context_tools
        self._policies = policy_provider or DEFAULT_POLICIES

    def build(self):
        graph = StateGraph(MainDecisionState)
        graph.add_node("load_decision_context", self._load_context)
        graph.add_node("select_candidate_capabilities", self._select_candidates)
        graph.add_node("infer_main_decision", self._infer)
        graph.add_node("read_card_context", self._read_context)
        graph.add_node("validate_decision_capability", self._validate_capability)
        graph.add_node("validate_decision_target", self._validate_target)
        graph.add_node("assemble_agent_handoff", self._assemble_handoff)
        graph.add_node("record_decision_audit", self._audit)
        graph.add_edge(START, "load_decision_context")
        graph.add_edge("load_decision_context", "select_candidate_capabilities")
        graph.add_edge("select_candidate_capabilities", "infer_main_decision")
        graph.add_conditional_edges("infer_main_decision",
            lambda state: "read" if state.get("context_request") else "validate",
            {"read": "read_card_context", "validate": "validate_decision_capability"})
        graph.add_conditional_edges("read_card_context",
            lambda state: "validate" if state.get("context_error") else "infer",
            {"validate": "validate_decision_capability", "infer": "infer_main_decision"})
        graph.add_conditional_edges(
            "validate_decision_capability", self._route_target,
            {"validate_target": "validate_decision_target", "assemble": "assemble_agent_handoff"},
        )
        graph.add_edge("validate_decision_target", "assemble_agent_handoff")
        graph.add_edge("assemble_agent_handoff", "record_decision_audit")
        graph.add_edge("record_decision_audit", END)
        return graph.compile()

    async def _load_context(self, state):
        """不预取邮件引用；只在模型提出只读请求后加载卡片。"""
        return {"decision_references": [], "context_results": [], "context_request": {},
                "context_round": 0, "context_read_count": 0, "context_error": False,
                "repair_count": 0, "validation_issue_codes": [], "decision_duration_ms": 0,
                "execution_policy_version": self._policies.current(state).version}

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
        policy = self._policies.current(state).main_context
        if not state["candidate_capabilities"]:
            return {
                "main_decision": clarify("当前没有可用的领域能力，请稍后重试或联系管理员。")
                    .model_dump(mode="json"),
                "decision_outcome": "empty_catalog", "decision_duration_ms": 0,
            }
        if state.get("context_round", 0) >= policy.max_rounds:
            return {**self._clarification("上下文读取轮次已达到上限，请明确选择卡片或重新查询。",
                                         "context_budget_exceeded"), "context_request": {}}
        with get_tracer(__name__).start_as_current_span("agent.main.decide") as span:
            span.set_attribute("aegis.run_id", state["run_id"])
            span.set_attribute("gen_ai.provider.name", getattr(self._main_agent, "provider_name", "unknown"))
            span.set_attribute("gen_ai.request.model", getattr(self._main_agent, "model_name", "unknown"))
            try:
                kwargs = dict(
                    message=state.get("latest_user_message", ""), history=state.get("history", []),
                    capabilities=[item.model_dump(mode="json") for item in self._capabilities
                                  if capability_id(item) in state["candidate_capabilities"]],
                    references=state["decision_references"],
                )
                if self._context_tools is not None: # 告诉模型有哪些工具和已取得的信息
                    kwargs.update(context_tools={name: schema.model_json_schema() for name, schema in CONTEXT_SCHEMAS.items()},
                        context_results=state.get("context_results", []), remaining_context_reads=max(0, policy.max_reads-state.get("context_read_count", 0)))
                # 调用主 Agent 判断下一步
                # 返回值可能是： MainContextReadRequest：需要读取卡片。
                #             MainDecision：已经决定交接、回复或澄清。
                decision = await self._main_agent.decide(**kwargs)
                if isinstance(decision, MainContextReadRequest):
                    metadata = self._metadata(state)
                    # 如果请求读取卡片，检查局部额度
                    if self._context_tools is None or state.get("context_read_count", 0) >= policy.max_reads:
                        return {**self._clarification("卡片读取不可用或已达到上限，请明确选择邮件或重新查询。",
                            "context_budget_exceeded"), "context_request": {}, **metadata,
                            "decision_duration_ms": state.get("decision_duration_ms", 0)+round((monotonic()-started)*1000)}
                    # 额度允许，登记请求给后续节点执行
                    return {"context_request": decision.model_dump(mode="json"),
                            "context_round": state.get("context_round", 0)+1, **metadata,
                            "decision_duration_ms": state.get("decision_duration_ms", 0)+round((monotonic()-started)*1000)}
                if decision.decision == "delegate" and decision.domain == "mail" and decision.intent not in {"query_mail", "arrange_meeting_from_mail"}:
                    # 代码保证自然语言只导航卡片，不能触发摘要或起草。
                    decision = MainDecision(
                        decision="delegate", domain="mail", intent="query_mail",
                        instruction=decision.instruction, target=decision.target, card_reference=decision.card_reference,
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
            "decision_duration_ms": state.get("decision_duration_ms", 0)+round((monotonic() - started) * 1000),
            "context_request": {}, "context_round": state.get("context_round", 0)+1,
            **self._metadata(state),
        }

    def _metadata(self, state):
        current = getattr(self._main_agent, "output_metadata", {})
        return {"repair_count": state.get("repair_count", 0)+current.get("repair_count", 0),
                "validation_issue_codes": current.get("validation_issue_codes", [])}

    async def _read_context(self, state):
        """实际读取卡片节点"""
        # 还原并校验请求
        profile = self._policies.current(state)  # 先核对恢复策略，再允许工具执行。
        request = MainContextReadRequest.model_validate(state["context_request"])
        try:
            # 执行只读工具
            payload = await self._context_tools.execute(_run(state), request)
        except ToolError as error:
            return {**self._clarification("卡片不存在、已失效或无法读取，请重新查询或选择卡片。",
                         "context_read_failed"), "context_error": True, "context_request": {},
                    "context_read_count": state["context_read_count"]+1}
        results = [*state["context_results"], payload] # 成功结果追加到已有上下文
        if len(json.dumps(results, ensure_ascii=False).encode()) > profile.main_context.max_context_bytes:
            return {**self._clarification("卡片上下文过大，请缩小范围后重试。", "context_read_failed"),
                    "context_error": True, "context_request": {}}
        return {"context_results": results, "context_request": {},
                "context_read_count": state["context_read_count"]+1}

    def _validate_capability(self, state):
        """模型只允许选择当前领域工厂显式声明的能力。"""
        decision = MainDecision.model_validate(state["main_decision"])
        if decision.decision == "delegate":
            if (decision.domain == "mail" and decision.target is None and decision.card_reference is None
                    and any("card_id" in item for item in state.get("context_results", []))):
                return self._clarification("已读取卡片但未明确有效目标，请选择对应邮件或重新查询。",
                                           "invalid_card_reference")
            try:
                capability = self._router.resolve_capability(decision, state["candidate_capabilities"])
            except WorkflowRoutingError as error:
                question = ("执行方向与当前任务不一致，请明确要处理日历还是邮件。"
                            if error.code == "outside_candidate_set" else
                            "当前尚不支持这项操作，请选择已有日历或邮件功能。")
                return self._clarification(question, error.code)
            if capability.requires_target and decision.target is None and decision.card_reference is None:
                return self._clarification("请明确选择要处理的邮件。", "missing_target")
        return {}

    @staticmethod
    def _route_target(state):
        decision = MainDecision.model_validate(state["main_decision"])
        return "validate_target" if decision.decision == "delegate" and (decision.target or decision.card_reference) else "assemble"

    async def _validate_target(self, state):
        """候选范围由图检查；归属和版本等业务事实由 Service 核验。"""
        decision = MainDecision.model_validate(state["main_decision"])
        resolved = None
        if decision.card_reference is not None and self._context_tools is not None:
            resolved = await self._context_tools.resolve(_run(state), decision.card_reference, state["context_results"])
        # 直接对象引用也必须与已经读取的卡片一致，不能仅凭模型知道某个ID就执行。
        elif decision.target is not None and self._context_tools is not None:
            from aegis_agent_worker.agent.contracts.context import CardReference
            for card in state["context_results"]:
                for item in card.get("items", []):
                    if item.get("email_id") == str(decision.target.id) and item.get("source_version") == decision.target.source_version:
                        reference = CardReference(card_id=card["card_id"], query_id=card["query_id"], position=item["position"])
                        resolved = await self._context_tools.resolve(_run(state), reference, state["context_results"])
        if decision.target is not None and resolved is not None:
            if ResourceReference.model_validate(resolved) != decision.target:
                resolved = None
        if resolved is None:
            return self._clarification("目标邮件不存在、已更新或无法确定，请重新选择要处理的邮件。",
                                       "invalid_target")
        decision = decision.model_copy(update={"target": ResourceReference.model_validate(resolved)})
        return {"main_decision": decision.model_dump(mode="json")}

    def _assemble_handoff(self, state):
        """构造专家输入和根图条件边输出，未通过校验的决策不能形成交接。"""
        decision = MainDecision.model_validate(state["main_decision"])
        handoff = None
        workflow = self._router.resolve_decision(decision, state["candidate_capabilities"])
        if decision.decision == "delegate":
            handoff = AgentHandoff(domain=decision.domain, intent=decision.intent,
                                   instruction=decision.instruction, target=decision.target)
        return {
            "handoff": handoff.model_dump(mode="json") if handoff else {},
            "intent": decision.intent or decision.decision, "workflow": workflow,
            "reply_context": state.get("context_results", []) if decision.decision == "reply" else [],
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
            context_read_count=state.get("context_read_count", 0),
            execution_policy_version=self._policies.current(state).version,
            fallback_reason=outcome if outcome != "validated" else None,
            referenced_cards=[{key: item[key] for key in ("card_id", "query_id", "status") if key in item}
                              for item in state.get("context_results", []) if "card_id" in item],
        )

    @staticmethod
    def _clarification(question, outcome):
        return {"main_decision": clarify(question).model_dump(mode="json"), "decision_outcome": outcome}


def _run(state):
    """从可信根图状态还原当前会话任务归属。"""
    return ClaimedRun(id=UUID(state["run_id"]), tenant_id=UUID(state["tenant_id"]),
                      user_id=UUID(state["user_id"]), conversation_id=UUID(state["conversation_id"]))
