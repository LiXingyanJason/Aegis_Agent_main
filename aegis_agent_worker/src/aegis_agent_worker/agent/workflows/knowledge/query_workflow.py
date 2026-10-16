"""
知识子图：规划／检索／证据复核／回答／来源持久化，职责分离。
在几个关键时间点反复检查同一件事：回答所依据的资料，现在是否仍可访问，且是否还是模型看到的那份内容。
"""
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
    def __init__(self, agent, query_service=None, evidence_service=None, artifacts=None, progress=None):
        self._agent, self._query_service = agent, query_service
        self._evidence, self._artifacts = evidence_service, artifacts
        self._progress=progress

    async def _expert_call(self,state,key,label,call):
        if self._progress is None:
            return await call()
        async with self._progress.phase(_run(state),key,label):
            return await call()

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
                "assistant_content": message, "knowledge_evidence": [], "knowledge_answer": {},
                "knowledge_retrieval": {}, "artifacts": []}

    async def _plan(self, state):
        """制定计划"""
        try:
            if state.get("run_type") != "conversation":
                raise ValueError("知识查询第一版仅支持普通对话入口")
            handoff = AgentHandoff.model_validate(state["handoff"])
            if handoff.domain != "knowledge" or handoff.intent != "query_knowledge" or handoff.target is not None:
                raise ValueError("交接不匹配")
            # _expert_call 调用 KnowledgeAgent.plan_query 让 LLM 根据最新用户消息生成查询计划
            # 同时记录“正在规划知识查询”这一步。
            raw = await self._expert_call(state,'knowledge_planning','正在规划知识查询',
                lambda:self._agent.plan_query(state.get('latest_user_message',''),handoff))
            # 校验计划的结构和字段
            plan = KnowledgeQueryPlan.model_validate(raw.model_dump() if hasattr(raw, "model_dump") else raw)
        except (ValueError, KeyError):
            return self._stop("知识查询计划未通过校验，尚未查询资料。请明确资料问题。",
                              "KNOWLEDGE_INVALID_PLAN", "invalid_plan")
        return {"knowledge_plan": plan.model_dump(mode="json"), "plan": plan.model_dump(mode="json"),
                "knowledge_error": "", "knowledge_error_code": "", "knowledge_evidence": [],
                "knowledge_answer": {}, "knowledge_retrieval": {}, "knowledge_status": "planned"}

    async def _search(self, state):
        if self._query_service is None:
            return self._stop("知识库检索尚未配置，无法查询公司政策或项目资料。", "KNOWLEDGE_NOT_CONFIGURED")
        try:
            # 检查计划是否符合查询契约，例如 query 非空且不超长
            plan = KnowledgeQueryPlan.model_validate(state["knowledge_plan"])
            run = _run(state) # 把图 State 中的任务字段组装成一个 ClaimedRun 对象，方便传给检索服务
            # 真正调用 query_service.query(run, plan)
            # 检查检索服务返回的数据能否构成合法结果，包括证据字段及检索诊断信息的类型和约束
            result = KnowledgeSearchResult.model_validate(await self._query_service.query(run, plan))
            if not result.evidence:
                # 如果没有证据，返回“无有效依据”，同时保留检索诊断信息，例如无结果原因或降级状态
                return {**self._stop(NO_EVIDENCE, "KNOWLEDGE_NO_EVIDENCE", "insufficient_evidence"),
                        "knowledge_retrieval": result.retrieval.model_dump(mode="json") if result.retrieval else {}}
            if len(result.evidence) > plan.top_k:
                raise ValueError("超过请求结果上限")
        except ToolError as error:
            return self._stop("知识检索工具不可用，尚未查询或确认资料。", error.code)
        except (ValueError, KeyError):
            return self._stop("知识检索返回的证据无效，无法确认。", "KNOWLEDGE_INVALID_EVIDENCE")
        return {"knowledge_evidence": result.model_dump(mode="json")["evidence"], "knowledge_status": "retrieved",
                "knowledge_retrieval": result.retrieval.model_dump(mode="json") if result.retrieval else {}}

    async def _verified(self, state):
        """重新确认刚检索到的证据，此刻仍可供当前用户使用，而且内容没有变化
            防止检索完成后、回答交付前，资料状态发生变化"""
        if self._evidence is None:
            raise ToolError("KNOWLEDGE_NOT_CONFIGURED", "知识来源复核尚未配置")
        run = _run(state)
        result = KnowledgeSearchResult(evidence=state["knowledge_evidence"])
        # EvidenceService.verify。底层会重新查数据库，检查文档是否仍可见、已启用、未删除，以及片段是否仍属于当前有效版本；
        # 还会比较标题、摘录、位置和哈希等内容。失效或变化的证据不会被返回。
        rows = await self._evidence.verify(ToolContext(run.tenant_id, run.user_id, run.id), result.evidence)
        return KnowledgeSearchResult(evidence=rows)

    async def _verify(self, state):
        """把检索结果复核后，整理成可交给回答模型的证据"""
        try:
            # 调用 _verified(state)，重新检查每条证据的权限、有效版本和内容。
            result = await self._verified(state)
            if not result.evidence:
                return self._stop(NO_EVIDENCE, "KNOWLEDGE_NO_EVIDENCE", "insufficient_evidence")
        except ToolError as error:
            return self._stop("知识证据暂时无法核验，无法确认资料。", error.code)
        except ValueError:
            return self._stop("知识证据校验失败，无法确认资料。", "KNOWLEDGE_INVALID_EVIDENCE")
        # 首次复核可能丢弃部分候选；回答前重新连续编号，不能把旧编号交给模型猜测。
        rows = [item.model_copy(update={'source_no':index}) for index,item in enumerate(result.evidence,1)]
        # 如果仍有证据，它会按保留下来的顺序重新编号 [1]、[2]、[3]
        return {"knowledge_evidence": [item.model_dump(mode='json') for item in rows], "knowledge_status": "verified"}

    async def _answer(self, state):
        """让 KnowledgeAgent 根据已复核的证据生成回答"""
        try:
            result = KnowledgeSearchResult(evidence=state["knowledge_evidence"])
            # 模型收到查询词和证据，返回结构化答案
            raw = await self._expert_call(state,'knowledge_answer','正在生成来源约束回答',
                lambda:self._agent.generate_answer(state['knowledge_plan']['query'],result.evidence))
            # 检查答案本身的格式和内部一致性
            answer = KnowledgeAnswer.model_validate(raw.model_dump() if hasattr(raw, "model_dump") else raw)
            # 答案引用的每个编号，是否真的存在于本次已复核的证据中。例如证据只有 [1]、[2]，答案引用 [3] 就会被拒绝。
            answer.verify_sources(result.evidence)
            if not answer.can_confirm:
                return self._stop(NO_EVIDENCE, "KNOWLEDGE_NO_EVIDENCE", "insufficient_evidence")
        except (ValueError, KnowledgeOutputError):
            return self._stop("回答或来源引用未通过校验，无法安全确认。", "KNOWLEDGE_INVALID_ANSWER")
        return {"knowledge_answer": answer.model_dump(mode="json"), "knowledge_status": "answered"}

    async def _persist(self, state):
        """
        确认回答依据仍有效，保存来源卡片，然后生成给用户看的最终文本
        在把回答交给用户之前，再确认模型刚才使用的资料仍是同一份、当前用户仍有权查看
        生成期间资料被禁用、删除、收回共享，或片段正文发生变化，这份回答就不能继续作为“有来源的回答”交付
        """
        if self._artifacts is None: # 检查来源卡片服务是否已配置
            return self._stop("知识来源展示服务尚未配置，无法交付可核对的回答。", "KNOWLEDGE_NOT_CONFIGURED")
        try:
            # 重新校验 knowledge_answer，确认引用仍指向有效证据
            evidence = await self._verified(state)
            prior = KnowledgeSearchResult(evidence=state['knowledge_evidence'])
            if {item.chunk_id:item for item in evidence.evidence}!={item.chunk_id:item for item in prior.evidence}:
                # 即使失效项未被模型显式引用，也不能证明其内容没有影响回答。保守拒绝整个旧回答。
                raise ValueError('生成期间证据发生变化')
            answer = KnowledgeAnswer.model_validate(state["knowledge_answer"]).verify_sources(evidence.evidence)
        except (ValueError, ToolError):
            return self._stop("资料已失效或无法读取，请重新查询。", "KNOWLEDGE_SOURCE_UNAVAILABLE")
        sources = [item for item in evidence.evidence if item.source_no in answer.citation_numbers]
        # 返回 Artifact、最终回复和 "succeeded" 状态，由 LangGraph 更新 State。
        artifact = KnowledgeSourceArtifact(payload=KnowledgeSourcesPayload(sources=sources,has_conflict=answer.has_conflict)).to_artifact()
        await self._artifacts.persist(_run(state), [artifact])
        content = answer.answer
        if answer.has_conflict:
            content = '资料存在冲突，以下列出不同依据，不能据此确认唯一结论。\n\n' + content
        if state.get('knowledge_retrieval',{}).get('degraded'):
            content = '本次向量检索不可用，以下仅依据关键词检索结果回答。\n\n' + content
        return {"artifacts": [artifact.model_dump(mode="json")], "assistant_content": content,
                "knowledge_evidence": [item.model_dump(mode='json') for item in evidence.evidence],
                "knowledge_status": "succeeded"}


def _run(state):
    """把图 State 中的任务字段组装成一个 ClaimedRun 对象，方便传给检索服务"""
    return ClaimedRun(id=UUID(state["run_id"]), tenant_id=UUID(state["tenant_id"]), user_id=UUID(state["user_id"]),
                     conversation_id=UUID(state["conversation_id"]), run_type="conversation")
