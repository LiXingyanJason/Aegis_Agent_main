"""唯一协同链路的业务、安全与同run恢复；无外部服务。"""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID
import pytest
from pydantic import ValidationError
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command
from aegis_agent_worker.agent.contracts.calendar import CalendarDecision
from aegis_agent_worker.agent.contracts.cooperation import TaskStepInput,TaskStepResult,verify_grounded_calendar,snapshot_digest
from aegis_agent_worker.agent.contracts.decision import MainDecision,ResourceReference
from aegis_agent_worker.agent.specialists.mail_agent import MailAgent,MailAgentError
from aegis_agent_worker.agent.specialists.calendar_agent import CalendarAgent
from aegis_agent_worker.agent.specialists.main_agent import MainAgent
from aegis_agent_worker.agent.specialists.cooperation_planner import CooperationPlanner
from aegis_agent_worker.agent.workflows.cooperation.factory import CooperationWorkflowFactory
from aegis_agent_worker.agent.graph.task_graph import TaskGraph,TaskGraphDependencies
from aegis_agent_worker.agent.graph.graph_registry import GraphRegistry
from aegis_agent_worker.harness.policy_provider import LEGACY_POLICY,COOPERATION_POLICY,COOPERATION_KNOWLEDGE_POLICY
from aegis_agent_worker.service.cooperation.cooperation_service import CooperationService
from aegis_agent_worker.repository.cooperation_repository import CooperationRepository
from aegis_agent_worker.tool.contracts import ToolError

EXCERPT='请在2026-10-26 09:00 至2026-10-26 17:00之间安排50分钟会议。'
REQUEST='请按续约邮件的时间要求查询日历并安排会议草稿。'
PLAN=dict(action='prepare_meeting',time_mode='search_window',timezone='Asia/Shanghai',
    start_at='2026-10-26T09:00:00+08:00',end_at='2026-10-26T17:00:00+08:00',duration_minutes=50,title='续约沟通',attendees=[])
EMAIL=dict(provider_message_id='renewal-1',source_version='v1',body_text='DO_NOT_CHECKPOINT_FULL_BODY\n'+EXCERPT)
SOURCE=ResourceReference(resource_type='email',id=UUID(int=5),source_version='v1')
SLOT=dict(slot_id='slot_abcdef01',start_at='2026-10-26T10:00:00+08:00',end_at='2026-10-26T12:00:00+08:00')


class Client:
    provider_name='fake'
    model_name='fake-model'
    def __init__(self,*responses):
        self.complete=AsyncMock(side_effect=list(responses))
        self.complete_json=self.complete


def plan():
    return {'version':1,'steps':[
        {'step_id':'lookup','workflow':'cooperation_mail_requirements','depends_on':[],
            'inputs':[{'kind':'task','name':'user_request'}],'expected_output':'MailRequirementsV1'},
        {'step_id':'slots','workflow':'cooperation_calendar_candidates','depends_on':['lookup'],
            'inputs':[{'kind':'step','step_id':'lookup','output_contract':'MailRequirementsV1'}],'expected_output':'CalendarCandidatesV1'},
        {'step_id':'draft','workflow':'cooperation_meeting_draft','depends_on':['lookup','slots'],
            'inputs':[{'kind':'step','step_id':'lookup','output_contract':'MailRequirementsV1'},
                      {'kind':'step','step_id':'slots','output_contract':'CalendarCandidatesV1'}],'expected_output':'MeetingDraftV1'},
    ]}



class ModelPlanningFactory(CooperationWorkflowFactory):
    """历史模型规划用例显式覆盖模型分支；生产不提供强制LLM的配置开关。"""
    def build_registry(self,steps):
        registry=super().build_registry(steps)
        registry.unique_plan=lambda **kwargs:None
        return registry


def setup(*,plan_response=None):
    mail_client=Client(json.dumps(dict(action='query',query={'action':'filter','keywords':['续约']})),json.dumps({'excerpt':EXCERPT},ensure_ascii=False))
    calendar_client=Client(json.dumps(PLAN),json.dumps(dict(title='续约沟通',attendees=[],selected_slot_id=SLOT['slot_id'],reason='可信候选可容纳50分钟')))
    mail,calendar=MailAgent(mail_client),CalendarAgent(llm_client=calendar_client)
    query=SimpleNamespace(query=AsyncMock(return_value=([dict(id=SOURCE.id,source_version='v1')],'mock')))
    source=SimpleNamespace(read_source=AsyncMock(return_value=dict(EMAIL)),verify_source=AsyncMock(),record_waiting=AsyncMock(),record_plan=AsyncMock(),
        check_execution=AsyncMock(),verify_draft=AsyncMock(),record_summary=AsyncMock(),record_progress=AsyncMock(),
        record_scheduled_step=AsyncMock(side_effect=lambda run,metadata,result:metadata))
    confirmation=SimpleNamespace(
        query_available_slots=AsyncMock(return_value={'availability':{'duration_minutes':50,'candidates':[SLOT]}}),
        create_recommended_draft=AsyncMock(return_value={'draft_id':str(UUID(int=7)),'approval_item_id':str(UUID(int=8)),
            'awaiting_confirmation':True,'assistant_content':'请核对会议草稿'}),
        create_approved_event=AsyncMock(return_value='external-1'))
    responses=[json.dumps(dict(decision='delegate',domain='mail',intent='arrange_meeting_from_mail',instruction=REQUEST))]
    responses.append(json.dumps(plan()) if plan_response is None else plan_response)
    main_client=Client(*responses)
    factory=ModelPlanningFactory(mail,query,calendar,confirmation,source,CooperationPlanner(main_client))
    main=MainAgent(main_client)
    context=SimpleNamespace(load_history=AsyncMock(return_value=[{'role':'user','content':REQUEST}]))
    dependencies=TaskGraphDependencies(context_service=context,lifecycle=object(),llm_client=Client(),system_prompt='test',
        workflow_factories=(factory,),main_agent=main,decision_context_service=SimpleNamespace(record_decision=AsyncMock()))
    checkpointer=MemorySaver()
    graph=TaskGraph(dependencies).build(checkpointer=checkpointer)
    state={key:str(UUID(int=index)) for index,key in enumerate(('run_id','tenant_id','user_id','conversation_id'),1)}
    state['run_type']='conversation'
    state['execution_policy_version']=COOPERATION_POLICY.version
    return SimpleNamespace(graph=graph,state=state,config={'configurable':{'thread_id':state['run_id']}},
        source=source,query=query,confirmation=confirmation,mail_client=mail_client,calendar_client=calendar_client,factory=factory,
        main_client=main_client,dependencies=dependencies,checkpointer=checkpointer)


@pytest.mark.asyncio
@pytest.mark.parametrize('decision',['approved','rejected'])
async def test_root_cooperation_chain_pauses_then_resumes_same_run_without_replanning(decision):
    env=setup()
    paused=await env.graph.ainvoke(env.state,env.config)
    assert '__interrupt__' in paused, paused
    assert paused['run_id']==env.state['run_id']
    snapshot=env.graph.get_state(env.config,subgraphs=True)
    # 暂停时子图尚未向根图提交输出；恢复状态保存在同一thread的嵌套Checkpoint。
    while snapshot.tasks and getattr(snapshot.tasks[0].state,'values',None) is not None:
        snapshot=snapshot.tasks[0].state
    current=snapshot.values
    assert set(current['cooperation_results'])=={'mail_requirements','calendar_candidates'}
    assert current['cooperation_pending_result']['status']=='waiting_confirmation'
    assert current['cooperation_results']['calendar_candidates']['candidate_version']==snapshot_digest(current['availability'])
    env.confirmation.create_approved_event.assert_not_awaited()
    # State/Checkpoint/步骤审计不得保存原邮件或提取摘录。
    assert EMAIL['body_text'] not in str(env.graph.get_state(env.config))
    assert EXCERPT not in str(current['cooperation_results'])
    assert EXCERPT not in str(env.source.record_waiting.await_args_list)
    counts=(env.mail_client.complete.await_count,env.calendar_client.complete.await_count)
    finished=await env.graph.ainvoke(Command(resume=decision),env.config)
    assert finished['cooperation_results']['meeting_draft']['status']==('completed' if decision=='approved' else 'rejected')
    assert counts==(env.mail_client.complete.await_count,env.calendar_client.complete.await_count)
    assert env.query.query.await_count==1 and env.confirmation.query_available_slots.await_count==1
    assert env.confirmation.create_recommended_draft.await_count==1
    assert env.confirmation.create_approved_event.await_count==(1 if decision=='approved' else 0)
    if decision=='approved':
        assert any(call.kwargs=={'fresh':True} for call in env.source.verify_source.await_args_list)
    assert all(call.args[0].id==UUID(env.state['run_id']) for call in env.source.record_waiting.await_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['none','ambiguous','stale_read','unclear','no_slots','forged_slot','tool_failed'])
async def test_failures_do_not_create_draft_or_external_write(mode):
    env=setup()
    if mode=='none': env.query.query.return_value=([],'mock')
    if mode=='ambiguous': env.query.query.return_value=([dict(id=SOURCE.id,source_version='v1')]*2,'mock')
    if mode=='stale_read': env.source.read_source.side_effect=ToolError('COOP_SOURCE_CHANGED','stale')
    if mode=='unclear': env.calendar_client.complete.side_effect=[json.dumps({'action':'clarify','clarification_question':'请给日期'})]
    if mode=='no_slots': env.confirmation.query_available_slots.return_value={'confirmation_error':'无可用时段'}
    if mode=='forged_slot': env.confirmation.query_available_slots.return_value={'availability':{'duration_minutes':50,
        'candidates':[{**SLOT,'start_at':'2026-10-27T10:00:00+08:00','end_at':'2026-10-27T12:00:00+08:00'}]}}
    if mode=='tool_failed': env.query.query.side_effect=ToolError('MAIL_CONNECTION_REQUIRED','missing')
    finished=await env.graph.ainvoke(env.state,env.config)
    assert not finished.get('__interrupt__') and finished['assistant_content']
    env.confirmation.create_recommended_draft.assert_not_awaited()
    env.confirmation.create_approved_event.assert_not_awaited()
    if mode not in {'no_slots','forged_slot'}: env.confirmation.query_available_slots.assert_not_awaited()


@pytest.mark.asyncio
async def test_revocation_before_approval_resume_blocks_write():
    env=setup()
    await env.graph.ainvoke(env.state,env.config)
    env.source.verify_source.side_effect=ToolError('COOP_SOURCE_CHANGED','revoked')
    done=await env.graph.ainvoke(Command(resume='approved'),env.config)
    assert done['cooperation_summary']['status']=='unavailable'
    env.confirmation.create_approved_event.assert_not_awaited()
    assert env.confirmation.create_recommended_draft.await_count==1


@pytest.mark.parametrize('changes',[{'duration_minutes':40},{'attendees':['attacker@example.com']},
    {'start_at':'2026-10-26T08:00:00+08:00'},{'start_at':'2026-10-26T09:00:01+08:00'}])
def test_ungrounded_constraints_rejected(changes):
    with pytest.raises(ValueError): verify_grounded_calendar(CalendarDecision(**{**PLAN,**changes}),EXCERPT,REQUEST)


def test_contracts_and_policies_are_bounded_and_old_scope_unchanged():
    with pytest.raises(ValidationError): TaskStepInput(step_id='arbitrary_tool',tenant_id=str(UUID(int=9)))
    with pytest.raises(ValidationError): TaskStepInput(step_id='mail_requirements',source={'resource_type':'email','id':str(UUID(int=5))})
    with pytest.raises(ValidationError): TaskStepResult(step_id='meeting_draft',status='unavailable',draft_id=UUID(int=7),issue_code='draft_unavailable')
    assert 'mail.messages.get' not in LEGACY_POLICY.execution.tools('conversation')
    assert 'mail.messages.get' in COOPERATION_POLICY.execution.tools('conversation')
    assert 'knowledge.search' in COOPERATION_KNOWLEDGE_POLICY.execution.tools('conversation')
    assert 'mail.messages.send' not in COOPERATION_POLICY.execution.tools('conversation')
    assert COOPERATION_POLICY.execution.max_tools==8
    registry=GraphRegistry();setup().factory.register(registry)
    assert registry.names()=={'mail_calendar_meeting'}
    assert registry.capabilities()[0].intent=='arrange_meeting_from_mail'


@pytest.mark.asyncio
async def test_mail_evidence_repair_is_bounded_and_cannot_invent_quote():
    client=Client(json.dumps({'excerpt':'伪造日期'}),json.dumps({'excerpt':'仍然伪造'}))
    with pytest.raises(MailAgentError): await MailAgent(client).extract_meeting_evidence(EMAIL)
    assert client.complete.await_count==2
    assert '伪造日期' not in str(client.complete.await_args)


@pytest.mark.asyncio
async def test_mail_evidence_satisfies_provider_json_prompt_requirement():
    """复现供应商对json_object缺少JSON字面词时的HTTP400校验，而非仅Mock模型结果。"""
    import httpx
    from aegis_agent_worker.config.settings import Settings
    from aegis_agent_worker.llm.client import OpenAICompatibleClient
    requests=[]
    def handle(request):
        payload=json.loads(request.content)
        requests.append(payload)
        assert payload['response_format']=={'type':'json_object'}
        if not any('json' in message['content'].lower() for message in payload['messages']):
            return httpx.Response(400,json={'error':{'message':'messages must contain json'}})
        return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps({'excerpt':EXCERPT},ensure_ascii=False)}}]})
    settings=Settings(_env_file=None,database_url='postgresql://unused',redis_url='redis://localhost:6379',
        model_provider='deepseek',model_api_base='https://model.example.com/v1',model_api_key='test-key',model_default_name='test-model')
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as transport:
        evidence=await MailAgent(OpenAICompatibleClient(settings,transport)).extract_meeting_evidence(EMAIL)
    assert evidence.excerpt==EXCERPT and len(requests)==1
    assert 'JSON Schema' in requests[0]['messages'][0]['content']


@pytest.mark.asyncio
async def test_repository_ownership_version_filter_uses_trusted_run():
    execute=AsyncMock(return_value=SimpleNamespace(mappings=lambda:SimpleNamespace(first=lambda:None)))
    run=SimpleNamespace(tenant_id=UUID(int=2),user_id=UUID(int=3))
    assert await CooperationRepository().email_source(SimpleNamespace(execute=execute),run,SOURCE) is None
    statement,values=execute.await_args.args
    for predicate in ('e.tenant_id=:tenant','e.user_id=:user','e.source_version=:version'): assert predicate in str(statement)
    assert values['tenant']==run.tenant_id and values['user']==run.user_id and values['version']=='v1'


@pytest.mark.asyncio
@pytest.mark.parametrize('owned,connected',[(False,True),(True,False),(True,True)])
async def test_source_service_gateway_ownership_connection_and_no_body_audit(owned,connected):
    from aegis_agent_worker.tool.gateway import ToolGateway
    from aegis_agent_worker.tool.registry import ToolRegistry
    from aegis_agent_worker.tool.contracts import ToolDefinition,ToolResult
    from aegis_agent_worker.repository.run_repository import ClaimedRun
    from test_mail_query_workflow import Database,Runs
    run=ClaimedRun(id=UUID(int=1),tenant_id=UUID(int=2),user_id=UUID(int=3),conversation_id=UUID(int=4))
    row=dict(id=SOURCE.id,source_version='v1',connection_id=UUID(int=6),provider_message_id=EMAIL['provider_message_id'])
    repository=SimpleNamespace(email_source=AsyncMock(return_value=row if owned else None))
    connections=SimpleNamespace(find_active_connection=AsyncMock(return_value=SimpleNamespace(id=UUID(int=6)) if connected else None))
    handler=SimpleNamespace(invoke=AsyncMock(return_value=ToolResult(response_payload=dict(EMAIL),output_summary={})))
    registry=ToolRegistry()
    registry.register(ToolDefinition(name='mail.messages.get',version='1',risk_level='read',mcp_server='email',description='read',
        connection_providers=('mock_mail',),required_connection_scope='mail.read'),handler)
    service=CooperationService(Database(),Runs(),ToolGateway(registry,connections=connections),repository=repository)
    calls=SimpleNamespace(create_running=AsyncMock(return_value=UUID(int=9)),succeed=AsyncMock(),fail=AsyncMock())
    service._calls=calls
    if not owned or not connected:
        with pytest.raises(ToolError): await service.read_source(run,SOURCE)
        handler.invoke.assert_not_awaited()
        return
    result=await service.read_source(run,SOURCE)
    assert result==EMAIL
    assert connections.find_active_connection.await_args.kwargs['required_connection_id']==UUID(int=6)
    assert connections.find_active_connection.await_args.kwargs['required_scope']=='mail.read'
    assert EXCERPT not in str(calls.succeed.await_args) and 'DO_NOT_CHECKPOINT' not in str(calls.succeed.await_args)
    await service.verify_source(run,SOURCE,snapshot_digest(EMAIL),fresh=True)
    handler.invoke.return_value=ToolResult(response_payload={**EMAIL,'body_text':'changed with same metadata version'},output_summary={})
    with pytest.raises(ToolError,match='邮件内容已变化'):
        await service.verify_source(run,SOURCE,snapshot_digest(EMAIL),fresh=True)


@pytest.mark.asyncio
async def test_evidence_repairs_pass_guarded_client_and_accumulate_audit():
    from test_harness_layers import Database,Repository
    from aegis_agent_worker.harness.executor import HarnessExecutor
    from aegis_agent_worker.harness.policy_provider import PolicyProvider
    from aegis_agent_worker.repository.run_repository import ClaimedRun
    from aegis_agent_worker.llm.guarded_client import GuardedLLMClient
    repo=Repository()
    harness=HarnessExecutor(Database(),repository=repo,policy_provider=PolicyProvider((COOPERATION_POLICY,),default_version=COOPERATION_POLICY.version))
    client=Client(json.dumps({'excerpt':'fabricated'}),json.dumps({'excerpt':'still fabricated'}))
    agent=MailAgent(GuardedLLMClient(client,harness))
    run=ClaimedRun(id=UUID(int=1),tenant_id=UUID(int=2),user_id=UUID(int=3),conversation_id=UUID(int=4))
    async with harness.scope(run):
        with pytest.raises(MailAgentError): await agent.extract_meeting_evidence(EMAIL)
    assert len(repo.reservations)==2 and len(repo.finished)==2
    assert {row['purpose'] for row in repo.finished}=={'cooperation_mail_evidence'}
    assert all(row['memory_count']==0 for row in repo.finished)
