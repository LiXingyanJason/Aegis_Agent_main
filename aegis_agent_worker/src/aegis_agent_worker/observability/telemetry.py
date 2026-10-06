"""OpenTelemetry 初始化与第三方库自动埋点。"""

from __future__ import annotations

from opentelemetry import trace
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.redis import RedisInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.sdk.resources import DEPLOYMENT_ENVIRONMENT, SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor

from aegis_agent_worker.config.database import Database
from aegis_agent_worker.config.settings import Settings

_provider_configured = False
_httpx_instrumented = False
_redis_instrumented = False
_sqlalchemy_instrumented = False


def configure_telemetry(settings: Settings, *, service_name: str) -> None:
    """初始化当前进程的 Trace Provider；默认不向外部导出，只保留上下文与 trace_id。"""
    global _provider_configured
    if not settings.otel_enabled or _provider_configured:
        return

    resource = Resource.create(
        {
            SERVICE_NAME: service_name,
            DEPLOYMENT_ENVIRONMENT: settings.app_env,
        }
    )
    provider = TracerProvider(resource=resource)
    if settings.otel_console_exporter:
        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
    trace.set_tracer_provider(provider)
    _provider_configured = True


def instrument_dependencies(database: Database) -> None:
    """为 HTTPX、Redis、SQLAlchemy 请求创建子 Span。"""
    global _httpx_instrumented, _redis_instrumented, _sqlalchemy_instrumented
    if not _httpx_instrumented:
        HTTPXClientInstrumentor().instrument()
        _httpx_instrumented = True
    if not _redis_instrumented:
        RedisInstrumentor().instrument()
        _redis_instrumented = True
    if not _sqlalchemy_instrumented:
        SQLAlchemyInstrumentor().instrument(engine=database.engine.sync_engine)
        _sqlalchemy_instrumented = True


def get_tracer(name: str):
    """返回模块专属 Tracer，供业务步骤创建手工 Span。"""
    return trace.get_tracer(name)
