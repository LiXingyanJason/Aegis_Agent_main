"""停用资料分类时保留历史列，并给新导入提供数据库兼容默认值。"""
from alembic import op

revision = '016_knowledge_category_default'
down_revision = '015_knowledge_version_binding'
branch_labels = depends_on = None


def upgrade():
    # 不改写已有资料；查询、API和索引均不再使用该旧列。
    op.execute("ALTER TABLE knowledge_documents ALTER COLUMN category SET DEFAULT 'project'")


def downgrade():
    op.execute('ALTER TABLE knowledge_documents ALTER COLUMN category DROP DEFAULT')
