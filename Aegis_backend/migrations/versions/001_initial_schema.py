"""建立 Aegis PA 当前业务 Schema、触发器与 RLS 策略。"""

from pathlib import Path

from migrations.sql_runner import execute_historical_sql

# revision identifiers, used by Alembic.
revision = "001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None

_ROOT_DIRECTORY = Path(__file__).resolve().parents[2]
_HISTORICAL_SCRIPT = _ROOT_DIRECTORY / "migrations" / "sql" / "001_initial_schema.sql"


def upgrade() -> None:
    """执行已纳入版本控制的初始 Schema 脚本。"""
    execute_historical_sql(_HISTORICAL_SCRIPT)


def downgrade() -> None:
    """初始 Schema 包含业务数据，禁止通过自动迁移整体删除。"""
    raise RuntimeError("001_initial_schema 是不可逆基线；请在独立数据库中手动清理后重新 upgrade。")
