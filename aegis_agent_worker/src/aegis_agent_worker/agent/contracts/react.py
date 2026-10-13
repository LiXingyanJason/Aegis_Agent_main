"""邮件起草信息收集的受限决定，不存储模型思维过程。"""
import re
from typing import Literal,Any
from pydantic import BaseModel,ConfigDict,Field,model_validator,field_validator


class ReActDecision(BaseModel):
    """
    信息收集循环中「模型决策」的结构化契约模型
    把 LLM 在 collect_information 里输出的下一步动作，严格约束成四种合法动作之一，
    并用跨字段校验 + 参数 Schema 二次校验 + 凭据过滤来保证决策安全、可持久化。
    """
    model_config=ConfigDict(extra="forbid")
    action:Literal["call_tool","ask_user","generate_draft","finish_unavailable"]
    tool_name:Literal["mail.messages.list","mail.messages.get","calendar.list_events","calendar.find_free_time"]|None=None
    arguments:dict[str,Any]=Field(default_factory=dict)
    question:str|None=Field(default=None,max_length=500)
    message:str|None=Field(default=None,max_length=500)
    @model_validator(mode="after")
    def validate_action(self):
        if self.action=="call_tool":
            if not self.tool_name or self.question or self.message:
                raise ValueError("工具决定组合无效")
            from aegis_agent_worker.tool.argument_schemas import SCHEMAS
            # 决定写入 Checkpoint 前先校验，Gateway 执行前仍再次校验。
            self.arguments=SCHEMAS[self.tool_name].model_validate(self.arguments).model_dump(mode="json",exclude_none=True)
        elif self.tool_name or self.arguments:
            raise ValueError("非工具决定不能包含调用参数")
        if self.action=="ask_user":
            if not self.question or not self.question.strip() or re.search("密码|token|密钥|secret|验证码",self.question,re.I):
                raise ValueError("补充问题无效或涉及凭据")
        elif self.question:
            raise ValueError("该动作不能提问")
        if self.action=="finish_unavailable" and not self.message:
            raise ValueError("结束必须说明缺失信息")
        return self


class RevisedBody(BaseModel):
    model_config=ConfigDict(extra="forbid")
    body:str=Field(min_length=1,max_length=65535)
    @field_validator("body")
    @classmethod
    def nonblank(cls,value):
        if not value.strip(): raise ValueError("正文不能为空白")
        return value
