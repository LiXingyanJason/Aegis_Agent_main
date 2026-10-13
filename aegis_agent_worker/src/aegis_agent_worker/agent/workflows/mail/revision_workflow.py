"""只修改已保存草稿正文的单次流程，不自动进入ReAct或发送。"""
from langgraph.graph import START,END,StateGraph
from aegis_agent_worker.agent.graph.state import TaskGraphState
from aegis_agent_worker.agent.workflows.mail.planning_workflow import mail_run


class MailRevisionWorkflow:
    """邮件草稿修改工作流,已保存的草稿正文按用户指令改写一遍并写回"""
    def __init__(self,agent,service):
        self._agent,self._service=agent,service
    def build(self):
        graph=StateGraph(TaskGraphState)
        graph.add_node("revise_saved_body",self._revise)
        graph.add_edge(START,"revise_saved_body")
        graph.add_edge("revise_saved_body",END)
        return graph.compile()
    async def _revise(self,state):
        run=mail_run(state)
        draft=await self._service.load(run)
        body=await self._agent.revise_body(draft["body"],draft["instruction"])
        await self._service.save(run,draft,body)
        return {"mail_workflow_completed":True}
