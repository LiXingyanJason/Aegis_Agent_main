"""知识规划/回答阶段进度；只处理事务与事件，不调用模型。"""
from contextlib import asynccontextmanager
from aegis_agent_worker.config.database import TenantContext,tenant_transaction


class KnowledgeWorkflowProgress:
    """
    记录知识工作流某个阶段的运行进度，例如“正在规划知识查询”或“正在生成来源约束回答”。
    记录 run_steps，并在配置了事件发布器时发送 progress_updated，供前端显示
    """
    def __init__(self,database,runs,events=None):
        self._database,self._runs,self._events=database,runs,events

    async def _event(self,session,run,step_id,label,status):
        if self._events is not None:
            event=await self._runs.append_event(session,run_id=run.id,tenant_id=run.tenant_id,
                event_type='progress_updated',payload={'step_id':str(step_id),'label':label,'status':status})
            self._events.publish_after_commit(event)

    @asynccontextmanager
    async def phase(self,run,key,label):
        context=TenantContext(run.tenant_id,run.user_id)
        async with self._database.session() as session:
            async with tenant_transaction(session,context):
                step_id=await self._runs.create_running_step(session,run_id=run.id,tenant_id=run.tenant_id,step_key=key,label=label)
                await self._event(session,run,step_id,label,'running')
        status='failed'
        try:
            yield
            status='succeeded'
        finally:
            # 模型与检索期间不占用SQL事务；事件只包含代码标签，不含正文或原始错误。
            async with self._database.session() as session:
                async with tenant_transaction(session,context):
                    await self._runs.finish_step(session,step_id=step_id,run_id=run.id,tenant_id=run.tenant_id,
                        status=status,detail='知识阶段已完成' if status=='succeeded' else '知识阶段未完成')
                    await self._event(session,run,step_id,label,status)
