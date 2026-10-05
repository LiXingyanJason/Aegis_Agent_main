"""修复 assistant/system/tool 消息的 client_message_id 唯一约束。"""

from pathlib import Path

from alembic import op

from migrations.sql_runner import execute_historical_sql

# revision identifiers, used by Alembic.
revision = "002_msg_client_id_uq"
down_revision = "001_initial_schema"
branch_labels = None
depends_on = None

_ROOT_DIRECTORY = Path(__file__).resolve().parents[2]
_HISTORICAL_SCRIPT = _ROOT_DIRECTORY / "migrations" / "sql" / "002_fix_message_client_id_unique_constraint.sql"


def upgrade() -> None:
    """仅对初始 Schema 应用消息幂等唯一约束修复。"""
    execute_historical_sql(_HISTORICAL_SCRIPT)


def downgrade() -> None:
    """恢复初始 Schema 的会话级 client_message_id 唯一约束。"""
    op.execute("DROP INDEX IF EXISTS ux_conversation_messages_client_message_id_present")
    op.create_unique_constraint(
        "conversation_messages_conversation_id_client_message_id_key",
        "conversation_messages",
        ["conversation_id", "client_message_id"],
    )
