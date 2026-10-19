"""Redis工作索引协议v1：TTL、CAS、用户代际失效；无正文/偏好。

API仅共享此Redis协议，不导入Worker包。所有key使用相同hash-tag兼容Redis Cluster。
"""
import asyncio
import json
from uuid import UUID


""" 
PUT_SCRIPT:安全写入 检查用户缓存的“代际”是否仍等于读取时的代际。若用户在此期间登出、缓存被失效，就拒绝这次旧写入，返回 0。
如果已有缓存，比较 sequence_no 和 snapshot_tick；新数据比旧数据落后时拒绝覆盖。
写入会话快照，并设置过期时间（TTL）。
把该会话缓存键加入有序集合；超过用户可保留的会话数时，淘汰最旧的键。
成功返回 1。

INVALIDATE_SCRIPT:负责用户级失效  先将用户代际加一，再删除索引中记录的会话缓存，最后删除索引。
"""

#  Redis 缓存时的底层执行脚本，由应用调用，实际在 Redis 服务器内执行

PUT_SCRIPT = """
if (redis.call('GET',KEYS[2]) or '0') ~= ARGV[1] then return 0 end
local old=redis.call('GET',KEYS[1])
if old then
  local ok,current=pcall(cjson.decode,old)
  if ok and type(current)=='table' and type(current.sequence_no)=='number' and type(current.snapshot_tick)=='number' then
    if current.sequence_no>tonumber(ARGV[3]) or current.snapshot_tick>tonumber(ARGV[4]) then return 0 end
  end
end
redis.call('SET',KEYS[1],ARGV[2],'EX',ARGV[5])
redis.call('ZADD',KEYS[3],ARGV[4],KEYS[1])
local excess=redis.call('ZCARD',KEYS[3])-tonumber(ARGV[6])
if excess>0 then
  local victims=redis.call('ZRANGE',KEYS[3],0,math.min(excess-1,31))
  local prefix=string.sub(KEYS[2],1,#KEYS[2]-5)
  for _,key in ipairs(victims) do
    if string.sub(key,1,#prefix)==prefix and #key==#prefix+36 then redis.call('DEL',key) end
    redis.call('ZREM',KEYS[3],key)
  end
end
redis.call('EXPIRE',KEYS[3],tonumber(ARGV[5])*2)
return 1
"""
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


def cache_keys(tenant_id, user_id, conversation_id=None):
    tenant_id,user_id=UUID(str(tenant_id)),UUID(str(user_id))
    base=f'aegis:session-memory:v1:{{{tenant_id}:{user_id}}}'
    return (base+':epoch',base+':keys',base+':'+str(UUID(str(conversation_id))) if conversation_id else None)


class RedisSessionMemoryCache:
    def __init__(self, redis, policy):
        self._redis,self._policy=redis,policy

    async def read(self, tenant, user, conversation, *, policy=None):
        """读取会话缓存 read() 底层用 MGET，并增加了解码、大小检查和 JSON 解析"""
        policy=policy or self._policy
        epoch,_,key=cache_keys(tenant,user,conversation)
        # epoch同时包裹缓存；即使跨命令发生失效，旧代数据也不会被使用。
        async with asyncio.timeout(policy.redis_timeout_seconds):
            generation,raw=await self._redis.mget(epoch,key)
        if isinstance(generation,bytes): generation=generation.decode()
        if raw is not None and len(raw.encode() if isinstance(raw,str) else raw)>policy.max_cache_bytes:
            return str(generation or '0'),{}
        try:
            payload=json.loads(raw) if raw else None
            return str(generation or '0'),payload if payload is None or isinstance(payload,dict) else {}
        except (ValueError,UnicodeError,RecursionError):
            return str(generation or '0'),{}

    async def put(self, tenant, user, conversation, generation, payload, *, policy=None):
        """
        写入会话缓存   put() 底层用 EVAL 执行 Lua 脚本；
        脚本再调用 Redis 的 GET、SET、ZADD 等命令，完成并发校验、过期和淘汰。
        """
        policy=policy or self._policy
        epoch,index,key=cache_keys(tenant,user,conversation)
        raw=json.dumps(payload,ensure_ascii=True,separators=(',',':'))
        if len(raw.encode())>policy.max_cache_bytes:
            raise ValueError('SESSION_CACHE_TOO_LARGE')
        async with asyncio.timeout(policy.redis_timeout_seconds):
            return await self._redis.eval(PUT_SCRIPT,3,key,epoch,index,generation,raw,
                payload['sequence_no'],payload['snapshot_tick'],policy.ttl_seconds,policy.max_user_sessions)

    async def invalidate_user(self, tenant, user):
        """让该用户的会话缓存失效"""
        epoch,index,_=cache_keys(tenant,user)
        async with asyncio.timeout(self._policy.redis_timeout_seconds):
            return await self._redis.eval(INVALIDATE_SCRIPT,2,epoch,index)
