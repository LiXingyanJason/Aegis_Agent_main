"""将用户已确认内容写入长期记忆的请求参数。"""

from uuid import UUID

from app.param.memory_create_param import MemoryCreateParam


class MemoryConfirmParam(MemoryCreateParam):
    """要求关联一个当前用户拥有的任务，来源固定记录为 user_confirmed。"""

    source_run_id: UUID
