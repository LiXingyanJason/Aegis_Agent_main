"""Calendar MCP 的服务间 Bearer Key 校验。"""

from fastapi import HTTPException, status


def validate_service_key(authorization: str | None, expected_api_key: str | None) -> None:
    """配置密钥时要求调用方提供完全匹配的 Bearer Key。"""
    if expected_api_key and authorization != f"Bearer {expected_api_key}":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Calendar MCP 服务认证失败")
