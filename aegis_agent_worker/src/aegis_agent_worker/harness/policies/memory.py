"""7.1契约边界额度；7.4接入前不改变已有运行的筛选/审计行为。"""
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MemoryContextPolicy:
    """记忆上下文额度设置"""
    version: str = 'memory-contract-v1'
    max_items: int = 5
    max_item_chars: int = 400
    max_total_chars: int = 1500
    max_content_bytes: int = 6000
    # 没有可信Token估算时不伪装成0；若配置Token预算则未知估算拒绝使用。
    max_estimated_tokens: int | None = None

    def __post_init__(self):
        if not self.version or len(self.version) > 64:
            raise ValueError('记忆策略版本无效')
        for name, maximum in (('max_items', 50), ('max_item_chars', 4000),
                              ('max_total_chars', 200000), ('max_content_bytes', 800000)):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError('记忆策略额度无效')
        if self.max_item_chars > self.max_total_chars:
            raise ValueError('单条额度不能超过总额度')
        if self.max_estimated_tokens is not None and (
            type(self.max_estimated_tokens) is not int or self.max_estimated_tokens < 1
        ):
            raise ValueError('记忆Token额度无效')
