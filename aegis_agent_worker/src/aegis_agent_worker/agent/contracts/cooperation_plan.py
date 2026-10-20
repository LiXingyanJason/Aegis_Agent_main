"""模型只提议有界计划；输入是符号引用，不接受身份、对象ID或工具参数。"""
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field

StepId = Annotated[str, Field(min_length=1,max_length=32,pattern=r'^[a-z][a-z0-9_]*$')]
OutputContract = Literal['MailRequirementsV1','CalendarCandidatesV1','MeetingDraftV1']


class TaskInputReference(BaseModel):
    model_config=ConfigDict(extra='forbid',frozen=True)
    kind: Literal['task']='task'
    name: Literal['user_request','verified_target']


class StepOutputReference(BaseModel):
    model_config=ConfigDict(extra='forbid',frozen=True)
    kind: Literal['step']='step'
    step_id: StepId
    output_contract: OutputContract


PlanInputReference=Annotated[TaskInputReference | StepOutputReference,Field(discriminator='kind')]


class CooperationStep(BaseModel):
    model_config=ConfigDict(extra='forbid',frozen=True)
    step_id: StepId
    workflow: str=Field(min_length=1,max_length=64,pattern=r'^[a-z][a-z0-9_]*$')
    depends_on: tuple[StepId,...]=Field(default=(),max_length=5)
    inputs: tuple[PlanInputReference,...]=Field(min_length=1,max_length=3)
    expected_output: OutputContract


class CooperationPlan(BaseModel):
    model_config=ConfigDict(extra='forbid',frozen=True)
    version: Literal[1]=1
    steps: tuple[CooperationStep,...]=Field(min_length=1,max_length=6)
