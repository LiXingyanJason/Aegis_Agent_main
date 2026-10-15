"""用户显式标记工作台邮件状态，不自动推断重要性或对方是否回复。"""

from pydantic import BaseModel, ConfigDict, StrictBool


class MailImportanceParam(BaseModel):
    model_config = ConfigDict(extra="forbid")
    is_important: StrictBool


class MailFollowUpParam(BaseModel):
    model_config = ConfigDict(extra="forbid")
    awaiting_reply: StrictBool
