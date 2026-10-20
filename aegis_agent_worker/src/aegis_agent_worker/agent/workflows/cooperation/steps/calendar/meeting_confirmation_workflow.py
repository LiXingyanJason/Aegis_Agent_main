"""会议确认步骤：受约束推荐、站内草稿、人工确认和批准版本写入。"""
from uuid import UUID
from langgraph.graph import StateGraph,START,END
from aegis_agent_worker.agent.graph.state import TaskGraphState
from aegis_agent_worker.agent.contracts.cooperation import TaskStepResult,snapshot_digest
from aegis_agent_worker.tool.contracts import ToolError
from ...runtime.plan_state import verify_plan_state,verify_step_inputs
from ...runtime.execution import guarded_step,execution_boundary
from ...runtime.results import stage_result,record_waiting,require_succeeded_result
from ...runtime.run_context import run_from
from .base import CalendarStepWorkflow


class MeetingConfirmationWorkflow(CalendarStepWorkflow):
    def build(self):
        graph=StateGraph(TaskGraphState)
        graph.add_node('generate',guarded_step(self._generate_draft,'meeting_draft',self._registry))
        graph.add_node('save',guarded_step(self._create_draft,'meeting_draft',self._registry))
        graph.add_node('wait',self._wait)
        graph.add_node('finish',guarded_step(self._finish,'meeting_draft',self._registry))
        graph.add_edge(START,'generate')
        graph.add_conditional_edges('generate',lambda state:'save' if state.get('calendar_meeting_draft') and not state.get('confirmation_error') else 'finish',
            {'save':'save','finish':'finish'})
        graph.add_edge('save','wait')
        graph.add_edge('wait','finish')
        graph.add_edge('finish',END)
        return graph.compile()

    async def _generate_draft(self,state):
        await execution_boundary(self._source_service,state)
        verify_step_inputs(state,self._registry,'meeting_draft')
        return await super()._generate_draft(state)
    async def _create_draft(self,state):
        await execution_boundary(self._source_service,state)
        verify_step_inputs(state,self._registry,'meeting_draft')
        source=require_succeeded_result(state,'mail_requirements')
        try:
            candidates=TaskStepResult.model_validate(state['cooperation_results']['calendar_candidates'])
            if candidates.status!='succeeded' or candidates.candidate_version!=snapshot_digest(state['availability']):
                raise ValueError('候选版本变化')
            await self._source_service.verify_source(run_from(state),source.source,source.source_digest)
        except (ValueError,ToolError):
            return {'confirmation_error':'交接来源或日历候选已变化，未创建草稿。'}
        update=await super()._create_draft(state)
        if update.get('awaiting_confirmation'):
            result=TaskStepResult(step_id='meeting_draft',status='waiting_confirmation',source=source.source,
                draft_id=UUID(update['draft_id']),approval_item_id=UUID(update['approval_item_id']))
            await record_waiting(self._source_service,state,result)
            return {**update,**stage_result(state,result,self._registry)}
        return update
    async def _finish(self,state):
        await execution_boundary(self._source_service,state)
        verify_plan_state(state,self._registry)
        if state.get('approval_decision')=='approved' and not state.get('confirmation_error'):
            verify_step_inputs(state,self._registry,'meeting_draft')
            source=require_succeeded_result(state,'mail_requirements')
            # 恢复不重新调用LLM或生成草稿；fresh读取只验证原邮件，最终写入仍仅用批准版本。
            await self._source_service.verify_source(run_from(state),source.source,source.source_digest,fresh=True)
        update=await super()._finish(state)
        if state.get('confirmation_error'):
            result=TaskStepResult(step_id='meeting_draft',status='unavailable',issue_code='draft_unavailable')
        else:
            result=TaskStepResult(step_id='meeting_draft',status='completed' if state.get('approval_decision')=='approved' else 'rejected',
                source=require_succeeded_result(state,'mail_requirements').source,draft_id=UUID(state['draft_id']),approval_item_id=UUID(state['approval_item_id']))
        return {**update,**stage_result(state,result,self._registry)}


