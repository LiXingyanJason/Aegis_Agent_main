"""请求邮件语义提取的参数。"""

from pydantic import BaseModel


class MailExtractionRequestParam(BaseModel):
    """refresh=true 时忽略同版本成功缓存，重新提交一次提取任务。"""

    refresh: bool = False
