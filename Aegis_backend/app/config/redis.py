"""仅用于短期、可重建状态的 Redis 客户端工厂。"""

from redis.asyncio import Redis

from app.config.settings import Settings


def create_redis_client(settings: Settings) -> Redis:
    """创建 UTF-8 Redis 客户端；调用方负责关闭连接。"""
    return Redis.from_url(settings.redis_url, decode_responses=True)

