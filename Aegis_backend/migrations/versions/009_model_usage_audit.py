"""逐次模型调用用量及安全用途标签；旧记录保持未知值。"""
from alembic import op
import sqlalchemy as sa

revision = "009_model_usage_audit"
down_revision = "008_agent_harness"
branch_labels = depends_on = None


def upgrade():
    for name in ("purpose", "model_provider", "model_name"):
        op.add_column("run_execution_attempts", sa.Column(name, sa.Text(), nullable=True))
    for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
        op.add_column("run_execution_attempts", sa.Column(name, sa.BigInteger(), nullable=True))
        op.create_check_constraint(f"ck_attempts_{name}_nonnegative", "run_execution_attempts",
                                   f"{name} IS NULL OR {name} >= 0")


def downgrade():
    # 降级会丢失这些审计字段；先停止新版 Worker。
    for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
        op.drop_constraint(f"ck_attempts_{name}_nonnegative", "run_execution_attempts", type_="check")
        op.drop_column("run_execution_attempts", name)
    for name in ("purpose", "model_provider", "model_name"):
        op.drop_column("run_execution_attempts", name)
