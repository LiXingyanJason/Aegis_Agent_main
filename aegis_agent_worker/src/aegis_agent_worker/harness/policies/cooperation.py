"""协同计划、深度与串行额度；不替代Harness调用预算。"""
from dataclasses import dataclass
from .validation import positive_limits


@dataclass(frozen=True)
class CooperationPlanPolicy:
    version: str='cooperation-plan-v1'
    max_steps: int=6
    max_dependencies: int=5
    max_plan_bytes: int=8000
    max_step_result_bytes: int=8000
    max_results_bytes: int=24000
    max_depth: int=6
    max_parallel: int=1

    def __post_init__(self):
        positive_limits(self,('max_steps','max_dependencies','max_plan_bytes','max_step_result_bytes','max_results_bytes','max_depth','max_parallel'))
        if self.max_depth>6 or self.max_parallel!=1:
            raise ValueError('当前协同仅支持有界深度和串行执行')
        if not self.version or len(self.version)>64 or self.max_steps>6 or self.max_dependencies>5:
            raise ValueError('协同计划策略无效')
        if self.max_step_result_bytes>self.max_results_bytes:
            raise ValueError('单步状态不能超过总状态额度')
