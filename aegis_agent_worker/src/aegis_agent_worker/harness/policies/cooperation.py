"""8.2只限制计划和状态，不替代Harness调用预算或8.3调度策略。"""
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

    def __post_init__(self):
        positive_limits(self,('max_steps','max_dependencies','max_plan_bytes','max_step_result_bytes','max_results_bytes'))
        if not self.version or len(self.version)>64 or self.max_steps>6 or self.max_dependencies>5:
            raise ValueError('协同计划策略无效')
        if self.max_step_result_bytes>self.max_results_bytes:
            raise ValueError('单步状态不能超过总状态额度')
