"""仅清理工作缓存，Redis协议与Worker v1对齐；不导入Worker或操作业务数据。"""
import asyncio
from uuid import UUID


# 与Worker storage/session_memory_cache.py相同的有界失效协议。
INVALIDATE_SCRIPT = """
redis.call('INCR',KEYS[1])
local keys=redis.call('ZRANGE',KEYS[2],0,31)
local prefix=string.sub(KEYS[1],1,#KEYS[1]-5)
for _,key in ipairs(keys) do
  if string.sub(key,1,#prefix)==prefix and #key==#prefix+36 then redis.call('DEL',key) end
end
redis.call('DEL',KEYS[2])
return #keys
"""


class SessionMemoryService:
    def __init__(self, redis):
        self._redis=redis

    async def clear_user(self, user):
        tenant_id,user_id=UUID(str(user.tenant_id)),UUID(str(user.id))
        base=f'aegis:session-memory:v1:{{{tenant_id}:{user_id}}}'
        async with asyncio.timeout(1):
            await self._redis.eval(INVALIDATE_SCRIPT,2,base+':epoch',base+':keys')
