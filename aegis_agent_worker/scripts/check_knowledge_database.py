"""只读检查本地 PostgreSQL/知识表迁移；不输出凭据、不访问远端向量库。"""
import asyncio
import json
import asyncpg
from aegis_agent_worker.config.settings import get_settings


async def main():
    connection = await asyncpg.connect(get_settings().database_psycopg_url, timeout=5)
    try:
        revisions = await connection.fetch("SELECT version_num FROM alembic_version")
        role = await connection.fetchrow("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname=current_user")
        print(json.dumps({"server_version": await connection.fetchval("SHOW server_version"),
            "revisions": [dict(row) for row in revisions], "runtime_role": dict(role)}, ensure_ascii=False))
    finally:
        await connection.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        print("数据库检查失败；请检查连接配置及本地 PostgreSQL 是否启动（未输出凭据）。")
        raise SystemExit(1) from None
