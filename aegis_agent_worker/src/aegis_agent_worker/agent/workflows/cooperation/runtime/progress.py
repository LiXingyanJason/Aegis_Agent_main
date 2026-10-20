"""图层发出安全展示进度；不改变任务、审批或预算状态。"""
from .run_context import run_from
from .plan_state import require_engine


async def publish_progress(service,state,stage,status='running',*,phase=None,step_id=None):
    require_engine(state)
    if phase and step_id is None:
        step_id=state.get('cooperation_plan_binding',{}).get('phase_steps',{}).get(phase,phase)
    await service.record_progress(run_from(state),execution_mode='scheduled',
        stage=stage,status=status,phase=phase,step_id=step_id)
