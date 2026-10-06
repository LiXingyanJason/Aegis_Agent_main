"""Calendar MCP Server 启动入口。"""

import logging

from mcp.server.transport_security import TransportSecuritySettings

from aegis_calendar_mcp.config import get_settings
from aegis_calendar_mcp.observability.tracing import configure_logging
from aegis_calendar_mcp.server import create_server


def main() -> None:
    """按配置启动 stdio 或 Streamable HTTP 传输。"""
    configure_logging()
    settings = get_settings()

    mcp = create_server(settings)

    logger = logging.getLogger(__name__)
    if settings.transport == "stdio":
        logger.info("启动 Calendar MCP Server：transport=stdio provider=%s", settings.provider)
        mcp.run(transport="stdio")
        return
    logger.info("启动 Calendar MCP Server：http://%s:%s%s", settings.host, settings.port, settings.streamable_http_path)
    mcp.run(
        transport="streamable-http",
        host=settings.host,
        port=settings.port,
        streamable_http_path=settings.streamable_http_path,
        json_response=True,
        stateless_http=True,
        transport_security=TransportSecuritySettings(
            allowed_hosts=list(settings.allowed_hosts)
        ),
    )


if __name__ == "__main__":
    main()
