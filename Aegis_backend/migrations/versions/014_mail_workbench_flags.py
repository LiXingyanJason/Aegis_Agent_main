"""为今日工作台保存用户显式标记的邮件关注状态。"""

from alembic import op

revision = "014_mail_workbench_flags"
down_revision = "013_knowledge_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE email_messages ADD COLUMN is_important boolean NOT NULL DEFAULT false")
    op.execute("ALTER TABLE sent_mail_messages ADD COLUMN awaiting_reply boolean NOT NULL DEFAULT false")


def downgrade() -> None:
    op.execute("ALTER TABLE sent_mail_messages DROP COLUMN awaiting_reply")
    op.execute("ALTER TABLE email_messages DROP COLUMN is_important")
