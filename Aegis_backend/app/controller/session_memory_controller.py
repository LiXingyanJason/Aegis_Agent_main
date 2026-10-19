"""退出/主动重置会话工作缓存；没有模型、持久消息或长期记忆副作用。"""
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from redis.exceptions import RedisError

from app.security.dependencies import CurrentUser, get_current_user
from app.service.session_memory_service import SessionMemoryService


router=APIRouter(prefix='/session-memory',tags=['session-memory'])


@router.delete('',status_code=204)
async def clear_session_memory(request:Request,current_user:CurrentUser=Depends(get_current_user)):
    try:
        await SessionMemoryService(request.app.state.redis).clear_user(current_user.user)
    except (RedisError,TimeoutError):
        raise HTTPException(503,detail={'code':'SESSION_CACHE_UNAVAILABLE',
            'message':'工作缓存暂时无法清理；持久会话未删除，缓存将按TTL过期'},
            headers={'Cache-Control':'no-store'}) from None
    return Response(status_code=204,headers={'Cache-Control':'no-store'})
