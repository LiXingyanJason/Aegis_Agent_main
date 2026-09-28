"""统一的接口成功响应包装。"""

from typing import Any


def success(data: Any) -> dict[str, Any]:
    """将业务数据放入统一的 data 字段后返回。"""
    return {"data": data}
