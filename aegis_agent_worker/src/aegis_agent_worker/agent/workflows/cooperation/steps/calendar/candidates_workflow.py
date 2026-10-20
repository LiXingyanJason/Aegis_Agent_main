"""日历候选步骤：只消费已验证邮件结果，查询并约束真实候选。"""
import json
from datetime import datetime,timedelta
from aegis_agent_worker.agent.contracts.calendar import CalendarFreeTimeCandidate
from langgraph.graph import StateGraph,START,END
from aegis_agent_worker.agent.graph.state import TaskGraphState
from aegis_agent_worker.agent.contracts.decision import AgentHandoff
from aegis_agent_worker.agent.contracts.cooperation import TaskStepResult,snapshot_digest
from aegis_agent_worker.tool.contracts import ToolError
from ...runtime.plan_state import verify_step_inputs
from ...runtime.execution import guarded_step,execution_boundary
from ...runtime.results import stage_result,require_succeeded_result
from ...runtime.run_context import run_from
from .base import CalendarStepWorkflow


class CalendarCandidatesWorkflow(CalendarStepWorkflow):
    def build(self):
        graph=StateGraph(TaskGraphState)
        graph.add_node('prepare',guarded_step(self._prepare,'calendar_candidates',self._registry))
        graph.add_node('query',guarded_step(self._query_availability,'calendar_candidates',self._registry))
        graph.add_edge(START,'prepare')
        graph.add_edge('prepare','query')
        graph.add_edge('query',END)
        return graph.compile()

    async def _prepare(self,state):
        await execution_boundary(self._source_service,state)
        verify_step_inputs(state,self._registry,'calendar_candidates')
        source=require_succeeded_result(state,'mail_requirements')
        try:
            await self._source_service.verify_source(run_from(state),source.source,source.source_digest)
        except ToolError:
            return {'confirmation_error':'邮件来源已失效，未查询日历或创建草稿。'}
        decision=source.calendar_decision
        # 不让继承节点重新解释邮件；仅从已校验的结果转换内部日历交接。
        instruction=json.dumps({key:value for key,value in decision.model_dump(mode='json').items()
            if key in {'title','start_at','end_at','timezone','duration_minutes','time_mode'}},ensure_ascii=False)
        internal=AgentHandoff(domain='calendar',intent='arrange_meeting',instruction=instruction)
        return await super()._prepare({**state,'calendar_decision':decision.model_dump(mode='json'),
            'handoff':internal.model_dump(mode='json')})
    async def _query_availability(self,state):
        await execution_boundary(self._source_service,state)
        verify_step_inputs(state,self._registry,'calendar_candidates')
        update=await super()._query_availability(state)
        if update.get('availability'):
            availability=update['availability']
            try:
                decision=require_succeeded_result(state,'mail_requirements').calendar_decision
                candidates=[CalendarFreeTimeCandidate.model_validate(item) for item in availability['candidates'][:20]]
                if not candidates or availability.get('duration_minutes')!=decision.duration_minutes:
                    raise ValueError('候选时长缺失或变化')
                if len({item.slot_id for item in candidates})!=len(candidates): raise ValueError('重复候选')
                for item in candidates:
                    start,end=datetime.fromisoformat(item.start_at),datetime.fromisoformat(item.end_at)
                    if (start.tzinfo is None or end.tzinfo is None or not decision.start_at<=start<end<=decision.end_at
                            or end-start<timedelta(minutes=decision.duration_minutes)):
                        raise ValueError('候选不在原文窗口内')
            except (ValueError,TypeError,KeyError):
                return {'confirmation_error':'日历候选超出邮件时间要求或无法核验，未创建草稿。'}
            availability={**availability,'candidates':[item.model_dump(mode='json') for item in candidates]}
            result=TaskStepResult(step_id='calendar_candidates',status='succeeded',source=require_succeeded_result(state,'mail_requirements').source,
                candidate_ids=tuple(item.slot_id for item in candidates),candidate_version=snapshot_digest(availability))
            return {**update,'availability':availability,**stage_result(state,result,self._registry)}
        return update


