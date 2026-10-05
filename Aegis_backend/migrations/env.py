"""Alembic 异步迁移环境。"""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.config.settings import get_settings

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 当前 Schema 由 SQL 基线迁移维护，尚未建立完整 SQLAlchemy ORM metadata。
target_metadata = None


def _database_url() -> str:
    """统一复用项目 Settings，避免迁移工具与应用读取不同的数据库地址。"""
    return get_settings().database_async_url


def run_migrations_offline() -> None:
    """生成 SQL 文件时使用 PostgreSQL 方言，不建立数据库连接。"""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_schemas=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_migrations(connection: Connection) -> None:
    """在 Alembic 管理的事务内执行迁移版本。"""
    context.configure(connection=connection, target_metadata=target_metadata, include_schemas=True)
    with context.begin_transaction():
        context.run_migrations()


async def _run_migrations_async() -> None:
    """通过 asyncpg 驱动连接 PostgreSQL，再桥接给同步 Alembic Migration API。"""
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = _database_url()
    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    try:
        async with connectable.connect() as connection:
            await connection.run_sync(_run_migrations)
    finally:
        await connectable.dispose()


def run_migrations_online() -> None:
    """Alembic CLI 的在线迁移入口。"""
    asyncio.run(_run_migrations_async())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
