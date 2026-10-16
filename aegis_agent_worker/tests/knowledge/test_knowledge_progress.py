"""知识阶段SSE与事务边界；真实模型/数据库均替身。"""
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock,Mock
from uuid import UUID
import pytest
from aegis_agent_worker.service.knowledge.workflow_progress_service import KnowledgeWorkflowProgress
from test_knowledge_query_service import Database
from test_harness_layers import RUN
from test_knowledge_workflow import KnowledgeStore,Agent,Tools,Artifacts,state
from aegis_agent_worker.service.knowledge.evidence_service import EvidenceService
from aegis_agent_worker.agent.workflows.knowledge.factory import KnowledgeWorkflowFactory
from aegis_agent_worker.agent.graph.graph_registry import GraphRegistry


@pytest.mark.asyncio
@pytest.mark.parametrize('fail',[False,True])
async def test_progress_phase_transaction_free_and_safe_terminal_event(fail):
    database=Database()
    runs=SimpleNamespace(create_running_step=AsyncMock(return_value=UUID(int=100)),finish_step=AsyncMock(),append_event=AsyncMock())
    events=SimpleNamespace(publish_after_commit=Mock())
    progress=KnowledgeWorkflowProgress(database,runs,events)
    async def execute():
        async with progress.phase(RUN,'knowledge_answer','正在生成来源约束回答'):
            assert not database.active
            if fail: raise ValueError('不应进入事件的原文秘密')
    if fail:
        with pytest.raises(ValueError): await execute()
    else:
        await execute()
    assert runs.finish_step.await_args.kwargs['status']==('failed' if fail else 'succeeded')
    assert len(runs.append_event.await_args_list)==2
    assert '秘密' not in str(runs.append_event.await_args_list)
    assert events.publish_after_commit.call_count==2


@pytest.mark.asyncio
async def test_factory_injects_planning_answer_progress_and_keeps_existing_workflow():
    calls=[]
    class Progress:
        @asynccontextmanager
        async def phase(self,run,key,label):
            calls.append(key)
            yield
    store=KnowledgeStore()
    registry=GraphRegistry()
    KnowledgeWorkflowFactory(Agent(),Tools(store),EvidenceService(store),Artifacts(),progress_service=Progress()).register(registry)
    result=await registry.get('knowledge_query').ainvoke(state())
    assert result['knowledge_status']=='succeeded' and calls==['knowledge_planning','knowledge_answer']
