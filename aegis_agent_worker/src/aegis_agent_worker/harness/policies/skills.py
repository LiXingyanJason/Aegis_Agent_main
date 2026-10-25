from dataclasses import dataclass

@dataclass(frozen=True)
class SkillPolicy:
    enabled:bool=False
    max_candidates:int=20
    max_body_chars:int=6000
    max_task_chars:int=4000
    max_reads:int=1
    max_selection_rounds:int=1

    def __post_init__(self):
        if not 1<=self.max_candidates<=20 or not 1<=self.max_body_chars<=6000 or not 1<=self.max_task_chars<=4000:
            raise ValueError('Invalid Skill context limits')
        if self.max_reads!=1 or self.max_selection_rounds!=1:
            raise ValueError('Skill v1 permits one selection/read per run and purpose')
