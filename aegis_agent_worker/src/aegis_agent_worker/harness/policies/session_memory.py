"""会话工作缓存独立策略，不承载长期偏好或原始正文。"""
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SessionMemoryPolicy:
    version: str = 'session-work-v1'
    ttl_seconds: int = 1800
    max_user_sessions: int = 32
    max_tasks: int = 6
    max_cards: int = 4
    max_cache_bytes: int = 12000
    history_items: int = 20
    history_chars: int = 12000
    message_chars: int = 4000
    redis_timeout_seconds: float = 1.0

    def __post_init__(self):
        for name, maximum in (('ttl_seconds',86400),('max_user_sessions',32),('max_tasks',20),
            ('max_cards',10),('max_cache_bytes',64000),('history_items',20),
            ('history_chars',48000),('message_chars',4000)):
            value = getattr(self,name)
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError('会话缓存策略额度无效')
        if not self.version or len(self.version)>64 or not 0 < self.redis_timeout_seconds <= 5:
            raise ValueError('会话缓存策略配置无效')
