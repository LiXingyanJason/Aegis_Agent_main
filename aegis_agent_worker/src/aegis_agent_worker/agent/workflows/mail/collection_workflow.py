"""邮件起草中的有界信息收集循环与独立用户输入暂停。"""
import json
from hashlib import sha256
from uuid import UUID
from langgraph.graph import START,END,StateGraph
from langgraph.types import interrupt
from aegis_agent_worker.agent.graph.state import TaskGraphState
from aegis_agent_worker.agent.contracts.react import ReActDecision
from aegis_agent_worker.agent.workflows.mail.planning_workflow import mail_run
from aegis_agent_worker.service.mail.mail_collection_service import safe_evidence
from aegis_agent_worker.harness.exceptions import HarnessError
from aegis_agent_worker.tool.contracts import ToolError
from aegis_agent_worker.harness.policy_provider import DEFAULT_POLICIES


class MailCollectionWorkflow:
    """邮件起草前 有界信息收集 """
    def __init__(self,agent,tasks,tools,inputs,policy_provider=None):
        self._agent,self._tasks,self._tools,self._inputs=agent,tasks,tools,inputs
        self._policies = policy_provider or DEFAULT_POLICIES
    def build(self):
        graph=StateGraph(TaskGraphState)
        graph.add_node("decide_information",self._decide)
        graph.add_node("read_information",self._read)
        graph.add_node("prepare_user_question",self._prepare)
        graph.add_node("wait_user_answer",self._wait)
        graph.add_node("verify_collected_sources",self._verify)
        graph.add_node("stop_collection",self._stop)
        graph.add_edge(START,"decide_information")
        graph.add_conditional_edges("decide_information",lambda state:state["collection_decision"]["action"],
            {"call_tool":"read_information","ask_user":"prepare_user_question","generate_draft":END,
             "finish_unavailable":"stop_collection"})
        graph.add_edge("read_information","decide_information")
        graph.add_edge("prepare_user_question","wait_user_answer")
        graph.add_edge("wait_user_answer","verify_collected_sources")
        graph.add_edge("verify_collected_sources","decide_information")
        graph.add_edge("stop_collection",END)
        return graph.compile()

    async def _decide(self,state):
        rounds=state.get("collection_round",0)
        profile = self._policies.current(state)
        if rounds>=profile.mail_collection.max_rounds: # 轮次预算
            raise HarnessError("REACT_BUDGET_EXCEEDED","信息收集轮次已耗尽，尚未生成草稿")
        source=await self._tasks.load_reply_source(mail_run(state)) # 加载 source,核验版本一致性
        if source["source_version"]!=state["mail_decision"]["target"]["source_version"]:
            raise HarnessError("MAIL_SOURCE_CHANGED","邮件已更新，请重新提交起草")
        # 组装给模型的上下文
        metadata={key:str(source.get(key) or "") for key in ("provider_message_id","subject","instruction")}
        metadata["key_points"]=source.get("key_points") or []
        metadata["remaining_decision_rounds"]=profile.mail_collection.max_rounds-rounds
        decision=await self._agent.collect_information(source=metadata,evidence=state.get("collected_evidence",[]),
                                                       answers=state.get("user_answers",[]))
        return {"collection_decision":decision.model_dump(mode="json"),"collection_round":rounds+1,
                "execution_policy_version": profile.version}

    async def _read(self,state):
        """
        执行工具读取工具
        提出调用计划 ToolInvocation，程序校验后执行
        """
        profile = self._policies.current(state)
        run=mail_run(state)
        source=await self._tasks.load_reply_source(run)
        decision=ReActDecision.model_validate(state["collection_decision"])
        evidence=list(state.get("collected_evidence",[]))
        if decision.tool_name=="mail.messages.get":
            allowed={source["provider_message_id"]}
            for item in evidence:
                for mail in item.get("data",{}).get("messages",[]):
                    if isinstance(mail,dict):
                        allowed.add(mail.get("provider_message_id"))
            if decision.arguments.get("provider_message_id") not in allowed:
                raise HarnessError("MAIL_REFERENCE_REQUIRED","只能读取已返回的邮件引用")
        try:
            raw=await self._tools.invoke(run,decision,source["connection_id"])
        except ToolError as error:
            raise HarnessError(error.code,error.message) from error
        data=safe_evidence(raw)
        if decision.tool_name=="mail.messages.get":
            data["summary"]=await self._agent.summarize_evidence(raw)
        evidence.append({"tool_name":decision.tool_name,"arguments":decision.arguments,
                         "source_hash":self._hash(raw),"data":data})
        if len(json.dumps(evidence,ensure_ascii=False).encode())>profile.mail_collection.max_evidence_bytes:
            raise HarnessError("CONTEXT_BUDGET_EXCEEDED","补充上下文过大")
        return {"collected_evidence":evidence}

    async def _prepare(self,state):
        self._policies.current(state)
        question=state["collection_decision"]["question"]
        request=await self._inputs.prepare(mail_run(state),question)
        return {"input_request":request,"input_request_id":request["question_id"],"waiting_input":True}

    def _wait(self,state):
        """暂停等回答节点"""
        self._policies.current(state)
        value=interrupt(state["input_request"])
        if not isinstance(value,dict) or value.get("question_id")!=state["input_request_id"]:
            raise HarnessError("INPUT_CONFLICT","回答与当前问题不匹配")
        response=value.get("response")
        if not isinstance(response,str) or not 1<=len(response)<=2000:
            raise HarnessError("INPUT_INVALID","补充回答格式无效")
        return {"waiting_input":False,"user_answers":[*state.get("user_answers",[]),response]}

    async def _verify(self,state):
        """等待后重新检查直接依赖的邮件和日历事实；变化时结束，不继续使用旧内容。"""
        self._policies.current(state)
        run=mail_run(state)
        source=await self._tasks.load_reply_source(run)
        for item in state.get("collected_evidence",[]):
            if item["tool_name"]=="mail.messages.get" or item["tool_name"].startswith("calendar."):
                decision=ReActDecision(action="call_tool",tool_name=item["tool_name"],arguments=item["arguments"])
                raw=await self._tools.invoke(run,decision,source["connection_id"])
                if self._hash(raw)!=item["source_hash"]:
                    raise HarnessError("COLLECTED_SOURCE_CHANGED","补充期间相关邮件或日历已变化，请重新起草")
        return {}

    @staticmethod
    def _hash(raw):
        return sha256(json.dumps(raw,sort_keys=True,ensure_ascii=False,separators=(",",":")).encode()).hexdigest()
    def _stop(self,state):
        raise HarnessError("INFORMATION_UNAVAILABLE",state["collection_decision"]["message"])
