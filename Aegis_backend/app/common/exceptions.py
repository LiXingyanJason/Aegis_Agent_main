"""应用自定义异常。"""


class TenantContextError(RuntimeError):
    """在缺少已认证租户事务上下文时访问租户数据会抛出此异常。"""

