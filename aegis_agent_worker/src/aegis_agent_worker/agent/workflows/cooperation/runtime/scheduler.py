"""预构建串行协同图：静态分派、校验后提交、依赖阻断和确定性汇总。"""
from datetime import datetime, timezone
from langgraph.graph import END
from aegis_agent_worker.agent.contracts.cooperation import TaskStepResult
from aegis_agent_worker.agent.contracts.cooperation_outputs import OUTPUT_CONTRACTS
from aegis_agent_worker.agent.routing.cooperation_registry import CooperationPlanError
from aegis_agent_worker.harness.policy_provider import DEFAULT_POLICIES
from .run_context import run_from
from .plan_state import verify_plan_state, update_plan_progress,ENGINE_VERSION
from .progress import publish_progress


class CooperationScheduler:
    version=ENGINE_VERSION

    def __init__(self,registry_provider,service,policy_provider=None):
        self._registry=registry_provider
        self._service=service
        self._policies=policy_provider or DEFAULT_POLICIES

    def register(self,graph,steps):
        if any(self._registry().graphs().get(name) is not compiled for name,compiled in steps.items()) or set(steps)!=set(self._registry().graphs()):
            raise CooperationPlanError('step_graph_binding_mismatch')
        self._registry().assert_executable()
        # 所有分派边均来自代码注册，不接受模型构造节点或嵌套子计划。
        graph.add_node('begin_cooperation_schedule',self._begin)
        graph.add_node('validate_schedule',self._validate)
        graph.add_node('choose_next',self._choose)
        graph.add_node('verify_result',self._verify)
        graph.add_node('finish_cooperation',self._finish)
        graph.add_edge('validate_schedule','choose_next')
        graph.add_conditional_edges('choose_next',self._route,{**{key:key for key in steps},'finish':'finish_cooperation'})
        for name,subgraph in steps.items():
            graph.add_node(name,subgraph)
            graph.add_edge(name,'verify_result')
        graph.add_edge('verify_result','choose_next')
        graph.add_edge('finish_cooperation',END)

    def _begin(self,state):
        return {'cooperation_scheduler_version':self.version,'cooperation_pending_result':{},
                'cooperation_step_metrics':{},'cooperation_summary':{},'cooperation_failure_code':''}

    def _validated(self,state):
        if state.get('cooperation_scheduler_version')!=self.version:
            raise CooperationPlanError('scheduler_version_mismatch')
        return verify_plan_state(state,self._registry)

    def _validate(self,state):
        self._validated(state)
        if self._policies.current(state).cooperation.max_parallel!=1:
            raise CooperationPlanError('parallel_execution_blocked')
        return {}

    async def _choose(self,state):
        plan,binding=self._validated(state)
        await self._service.check_execution(run_from(state))
        by_id={item.step_id:item for item in plan.steps}
        progress={key:dict(state.get('cooperation_progress',{}).get(key,{'status':'pending','result_ref':None}))
                  for key in binding['step_order']}
        # 终态引用需对应真实已提交结果，不凭进度标志跳过步骤。
        for key,row in progress.items():
            if row['status'] in {'succeeded','completed','rejected'}:
                step=by_id[key]
                phase=self._registry().capability(step.workflow).phase
                result=OUTPUT_CONTRACTS[step.expected_output].model_validate(state.get('cooperation_results',{}).get(phase))
                if result.status!=row['status'] or row.get('result_ref')!='cooperation_results.'+phase:
                    raise CooperationPlanError('progress_result_mismatch')
        changed=True
        while changed:
            changed=False
            for key in binding['step_order']:
                if progress[key]['status']=='pending' and any(progress[parent]['status'] in
                        {'unavailable','blocked','rejected','cancelled'} for parent in by_id[key].depends_on):
                    progress[key]={'status':'blocked','result_ref':None,'reason':'dependency_failed'}
                    await publish_progress(self._service,state,'blocked','blocked',step_id=key,
                        phase=self._registry().capability(by_id[key].workflow).phase)
                    changed=True
        # 单步异常不重试；所有依赖失败分支都会确定性阻断。
        ready=next((key for key in binding['step_order'] if progress[key]['status']=='pending'
                    and all(progress[parent]['status']=='succeeded' for parent in by_id[key].depends_on)),None)
        if ready is None:
            if any(row['status'] in {'pending','running','waiting_confirmation'} for row in progress.values()):
                raise CooperationPlanError('schedule_stalled')
            return {'cooperation_progress':progress,'cooperation_current_step':''}
        metrics=dict(state.get('cooperation_step_metrics',{}))
        if ready in metrics: raise CooperationPlanError('step_reexecution_blocked')
        metrics[ready]={'workflow':by_id[ready].workflow,'started_at':datetime.now(timezone.utc).isoformat(),
                        'status':'running'}
        progress[ready]={'status':'running','result_ref':None}
        await publish_progress(self._service,state,'step_started',step_id=ready,
            phase=self._registry().capability(by_id[ready].workflow).phase)
        return {'cooperation_progress':progress,'cooperation_current_step':ready,'cooperation_status':'in_progress',
                'cooperation_pending_result':{},'cooperation_step_metrics':metrics,'cooperation_failure_code':''}

    def _route(self,state):
        key=state.get('cooperation_current_step')
        if not key: return 'finish'
        plan,_=self._validated(state)
        step=next(item for item in plan.steps if item.step_id==key)
        self._registry().capability(step.workflow)
        return step.workflow

    async def _checked_output(self,state,step,raw=None):
        phase=self._registry().capability(step.workflow).phase
        data=raw if raw is not None else state.get('cooperation_results',{}).get(phase)
        result=OUTPUT_CONTRACTS[step.expected_output].model_validate(data)
        if result.step_id!=phase: raise CooperationPlanError('step_result_contract_mismatch')
        if result.status=='unavailable': return result
        handler=self._registry().result_handler(step.expected_output)
        await handler.verify(self._service,state,result)
        return result

    async def _verify(self,state):
        plan,binding=self._validated(state)
        key=state['cooperation_current_step']
        step=next(item for item in plan.steps if item.step_id==key)
        phase=self._registry().capability(step.workflow).phase
        issue=self._registry().result_handler(step.expected_output).failure_issue
        code=state.get('cooperation_failure_code','')
        try:
            if not state.get('cooperation_pending_result'):
                raise CooperationPlanError('step_result_missing')
            result=await self._checked_output(state,step,state['cooperation_pending_result'])
            if result.status=='waiting_confirmation': raise CooperationPlanError('step_not_terminal')
            progress=update_plan_progress(state,result,self._registry)
        except (ValueError,KeyError) as error:
            result=TaskStepResult(step_id=phase,status='unavailable',issue_code=issue)
            code=getattr(error,'code','STEP_RESULT_INVALID')
            progress=update_plan_progress(state,result,self._registry)
        # ToolError意味着授权/来源已撤销，不交付原暂存结果。
        except Exception as error:
            from aegis_agent_worker.tool.contracts import ToolError
            if not isinstance(error,ToolError): raise
            result=TaskStepResult(step_id=phase,status='unavailable',issue_code=issue)
            code=error.code
            progress=update_plan_progress(state,result,self._registry)
        metrics=dict(state['cooperation_step_metrics'])
        metric={**metrics[key],'step_id':key,'status':result.status,'issue_code':code or result.issue_code,
                'source':result.source.model_dump(mode='json') if result.source else None,
                'result_ref':'cooperation_results.'+phase,'scheduler_version':self.version,
                'plan_digest':binding['plan_digest']}
        metric=await self._service.record_scheduled_step(run_from(state),metric,result)
        await publish_progress(self._service,state,'step_finished',result.status,step_id=key,phase=phase)
        metrics[key]=metric
        return {**progress,'cooperation_current_step':'','cooperation_pending_result':{},'cooperation_step_metrics':metrics,
            'cooperation_results':{**state.get('cooperation_results',{}),phase:result.model_dump(mode='json')},
            'cooperation_failure_code':code,
            'cooperation_error':result.issue_code if result.status=='unavailable' else ''}

    async def _finish(self,state):
        plan,binding=self._validated(state)
        progress=state['cooperation_progress']
        completed=[key for key in binding['step_order'] if progress[key]['status'] in {'succeeded','completed'}]
        failed=[key for key in binding['step_order'] if progress[key]['status']=='unavailable']
        blocked=[key for key in binding['step_order'] if progress[key]['status']=='blocked']
        rejected=any(row['status']=='rejected' for row in progress.values())
        status='unavailable' if failed or blocked else 'rejected' if rejected else 'completed'
        summary={'scheduler_version':self.version,'plan_digest':binding['plan_digest'],'status':status,
                 'completed_steps':completed,'failed_steps':failed,'blocked_steps':blocked,
                 'steps':[{'step_id':key,'workflow':next(step.workflow for step in plan.steps if step.step_id==key),
                           **progress[key],'issue_code':state.get('cooperation_step_metrics',{}).get(key,{}).get('issue_code')}
                          for key in binding['step_order']]}
        content=state.get('assistant_content','')
        if status=='completed':
            from aegis_agent_worker.agent.contracts.decision import AgentHandoff
            handoff=AgentHandoff.model_validate(state['handoff'])
            profile=self._registry().profile(handoff.domain,handoff.intent)
            # 目标达成由代码验证所有实际证据和终产物，不能由LLM或进度标记自报。
            checked=[]
            for step in plan.steps:
                try:
                    result=await self._checked_output(state,step)
                except (ValueError,KeyError,TypeError):
                    raise CooperationPlanError('goal_evidence_invalid') from None
                if result.status not in {'succeeded','completed'}:
                    raise CooperationPlanError('goal_result_not_ready')
                checked.append(dict(step_id=step.step_id,output_contract=step.expected_output,status='verified'))
            summary['goal_output']=profile.goal.output_contract
            summary['goal_checks']=checked
            handler=self._registry().result_handler(profile.goal.output_contract)
            if handler.present is not None:
                content=handler.present(state)
        if failed or blocked:
            content=f'协同任务未全部完成：已完成 {len(completed)} 步，失败 {len(failed)} 步，因依赖阻断 {len(blocked)} 步。未完成步骤不会被视为成功；未确认的会议不会写入日历。'
            if state.get('approval_decision')=='approved':
                content+='已批准写入的结果请核对执行记录，不要直接重复提交。'
        await self._service.record_summary(run_from(state),summary)
        await publish_progress(self._service,state,'finished',status)
        return {'cooperation_summary':summary,'cooperation_status':status,'assistant_content':content}
