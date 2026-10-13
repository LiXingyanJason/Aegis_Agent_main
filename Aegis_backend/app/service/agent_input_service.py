"""用户补充和草稿修改的业务入口，执行仍由 Worker 完成。"""
from app.repository.agent_input_repository import AgentInputRepository


class AgentInputService:
    def __init__(self,repository=None,publisher=None):
        self._repository=repository or AgentInputRepository()
        self._publisher=publisher
    async def list_inputs(self,session,user,run_id=None):
        rows,events=await self._repository.list_inputs(session,user,run_id)
        if self._publisher:
            for event in events: self._publisher.publish_after_commit(event)
        return rows
    async def answer(self,session,user,question_id,param):
        return await self._repository.answer(session,user,question_id,param)
    async def cancel(self,session,user,question_id):
        result=await self._repository.cancel(session,user,question_id)
        if isinstance(result,dict):
            event=result.pop("event")
            if self._publisher: self._publisher.publish_after_commit(event)
        return result
    async def create_revision(self,session,user,draft_id,param):
        return await self._repository.create_revision(session,user,draft_id,param)
