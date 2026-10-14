"""知识子图：规划／检索／证据复核／回答／来源持久化，职责分离。"""
from uuid import UUID
from langgraph.graph import StateGraph, START, END

from aegis_agent_worker.agent.contracts.decision import AgentHandoff
from aegis_agent_worker.agent.contracts.knowledge import (
    KnowledgeQueryPlan, KnowledgeSearchResult, KnowledgeAnswer, KnowledgeSourceArtifact, KnowledgeSourcesPayload,
)
from aegis_agent_worker.agent.workflows.knowledge.state import KnowledgeQueryState
from aegis_agent_worker.agent.specialists.knowledge_agent import KnowledgeOutputError
from aegis_agent_worker.repository.run_repository import ClaimedRun
from aegis_agent_worker.tool.contracts import ToolContext, ToolError

NO_EVIDENCE = "无法从已授权资料确认：没有找到有效依据，请补充资料或缩小查询范围。"


class KnowledgeQueryWorkflow:
    def __init__(self, agent, query_service=None, evidence_service=None, artifacts=None):
        self._agent, self._query_service = agent, query_service
        self._evidence, self._artifacts = evidence_service, artifacts

    def build(self):
        graph = StateGraph(KnowledgeQueryState)
        graph.add_node("plan_knowledge_query", self._plan)
        graph.add_node("search_knowledge", self._search)
        graph.add_node("verify_knowledge_evidence", self._verify)
        graph.add_node("answer_knowledge", self._answer)
        graph.add_node("persist_knowledge_sources", self._persist)
        graph.add_edge(START, "plan_knowledge_query")
        for current, following in (("plan_knowledge_query", "search_knowledge"),
            ("search_knowledge", "verify_knowledge_evidence"), ("verify_knowledge_evidence", "answer_knowledge"),
            ("answer_knowledge", "persist_knowledge_sources")):
            graph.add_conditional_edges(current, lambda state: "stop" if state.get("knowledge_error") else "next",
                                        {"stop": END, "next": following})
        graph.add_edge("persist_knowledge_sources", END)
        return graph.compile()

    @staticmethod
    def _stop(message, code, status="unavailable"):
        return {"knowledge_error": message, "knowledge_error_code": code, "knowledge_status": status,
                "assistant_content": message, "knowledge_evidence": [], "knowledge_answer": {}, "artifacts": []}

    async def _plan(self, state):
        try:
            if state.get("run_type") != "conversation":
                raise ValueError("知识查询第一版仅支持普通对话入口")
            handoff = AgentHandoff.model_validate(state["handoff"])
            if handoff.domain != "knowledge" or handoff.intent != "query_knowledge" or handoff.target is not None:
                raise ValueError("交接不匹配")
            raw = await self._agent.plan_query(state.get("latest_user_message", ""), handoff)
            plan = KnowledgeQueryPlan.model_validate(raw.model_dump() if hasattr(raw, "model_dump") else raw)
        except (ValueError, KeyError):
            return self._stop("知识查询计划未通过校验，尚未查询资料。请明确资料问题。",
                              "KNOWLEDGE_INVALID_PLAN", "invalid_plan")
        return {"knowledge_plan": plan.model_dump(mode="json"), "plan": plan.model_dump(mode="json"),
                "knowledge_error": "", "knowledge_error_code": "", "knowledge_evidence": [],
                "knowledge_answer": {}, "knowledge_status": "planned"}

    async def _search(self, state):
        if self._query_service is None:
            return self._stop("知识库检索尚未配置，无法查询公司政策或项目资料。", "KNOWLEDGE_NOT_CONFIGURED")
        try:
            plan = KnowledgeQueryPlan.model_validate(state["knowledge_plan"])
            run = _run(state)
            result = KnowledgeSearchResult.model_validate(await self._query_service.query(run, plan))
            if not result.evidence:
                return self._stop(NO_EVIDENCE, "KNOWLEDGE_NO_EVIDENCE", "insufficient_evidence")
            if len(result.evidence) > plan.top_k:
                raise ValueError("超过请求结果上限")
        except ToolError as error:
            return self._stop("知识检索工具不可用，尚未查询或确认资料。", error.code)
        except (ValueError, KeyError):
            return self._stop("知识检索返回的证据无效，无法确认。", "KNOWLEDGE_INVALID_EVIDENCE")
        return {"knowledge_evidence": result.model_dump(mode="json")["evidence"], "knowledge_status": "retrieved"}

    async def _verified(self, state):
        if self._evidence is None:
            raise ToolError("KNOWLEDGE_NOT_CONFIGURED", "知识来源复核尚未配置")
        run = _run(state)
        result = KnowledgeSearchResult(evidence=state["knowledge_evidence"])
        rows = await self._evidence.verify(ToolContext(run.tenant_id, run.user_id, run.id), result.evidence)
        return KnowledgeSearchResult(evidence=rows)

    async def _verify(self, state):
        try:
            result = await self._verified(state)
            if not result.evidence:
                return self._stop(NO_EVIDENCE, "KNOWLEDGE_NO_EVIDENCE", "insufficient_evidence")
        except ToolError as error:
            return self._stop("知识证据暂时无法核验，无法确认资料。", error.code)
        except ValueError:
            return self._stop("知识证据校验失败，无法确认资料。", "KNOWLEDGE_INVALID_EVIDENCE")
        return {"knowledge_evidence": result.model_dump(mode="json")["evidence"], "knowledge_status": "verified"}

    async def _answer(self, state):
        try:
            result = KnowledgeSearchResult(evidence=state["knowledge_evidence"])
            raw = await self._agent.generate_answer(state["knowledge_plan"]["query"], result.evidence)
            answer = KnowledgeAnswer.model_validate(raw.model_dump() if hasattr(raw, "model_dump") else raw)
            answer.verify_sources(result.evidence)
            if not answer.can_confirm:
                return self._stop(NO_EVIDENCE, "KNOWLEDGE_NO_EVIDENCE", "insufficient_evidence")
        except (ValueError, KnowledgeOutputError):
            return self._stop("回答或来源引用未通过校验，无法安全确认。", "KNOWLEDGE_INVALID_ANSWER")
        return {"knowledge_answer": answer.model_dump(mode="json"), "knowledge_status": "answered"}

    async def _persist(self, state):
        if self._artifacts is None:
            return self._stop("知识来源展示服务尚未配置，无法交付可核对的回答。", "KNOWLEDGE_NOT_CONFIGURED")
        try:
            # 回答生成后再复核，避免期间版本更新或撤权仍交付旧证据。
            evidence = await self._verified(state)
            answer = KnowledgeAnswer.model_validate(state["knowledge_answer"]).verify_sources(evidence.evidence)
        except (ValueError, ToolError):
            return self._stop("资料已失效或无法读取，请重新查询。", "KNOWLEDGE_SOURCE_UNAVAILABLE")
        sources = [item for item in evidence.evidence if item.source_no in answer.citation_numbers]
        artifact = KnowledgeSourceArtifact(payload=KnowledgeSourcesPayload(sources=sources)).to_artifact()
        await self._artifacts.persist(_run(state), [artifact])
        return {"artifacts": [artifact.model_dump(mode="json")], "assistant_content": answer.answer,
                "knowledge_status": "succeeded"}


def _run(state):
    return ClaimedRun(id=UUID(state["run_id"]), tenant_id=UUID(state["tenant_id"]), user_id=UUID(state["user_id"]),
                     conversation_id=UUID(state["conversation_id"]), run_type="conversation")
