"""上传、索引和启用分离；已有资料保留索引，但默认禁用，须所有者显式启用。"""
from alembic import op

revision = "013_knowledge_lifecycle"
down_revision = "012_knowledge_indexing"
branch_labels = depends_on = None


def upgrade():
    op.execute("ALTER TABLE knowledge_documents ADD COLUMN enabled boolean NOT NULL DEFAULT false")
    op.execute("ALTER TABLE knowledge_documents ADD COLUMN client_request_id varchar(128)")
    op.execute("ALTER TABLE knowledge_documents ADD COLUMN request_hash char(64)")
    # 保留旧上传的幂等身份，不重复登记源文件；不是重新索引或创建任务。
    op.execute("""UPDATE knowledge_documents d SET client_request_id=j.client_request_id,request_hash=j.request_hash
      FROM (SELECT DISTINCT ON(document_id) document_id,tenant_id,user_id,client_request_id,request_hash
        FROM knowledge_index_jobs WHERE client_request_id IS NOT NULL ORDER BY document_id,created_at,id) j
      WHERE d.id=j.document_id AND d.tenant_id=j.tenant_id AND d.user_id=j.user_id""")
    op.execute("CREATE UNIQUE INDEX uq_knowledge_upload_request ON knowledge_documents(tenant_id,user_id,client_request_id) WHERE client_request_id IS NOT NULL")
    # 上传时不需要模型配置；首次启动索引才固定模型空间。旧记录原值不变。
    for column in ("embedding_provider","embedding_model","embedding_model_version","embedding_dimensions"):
        op.execute(f"ALTER TABLE knowledge_document_versions ALTER COLUMN {column} DROP NOT NULL")


def downgrade():
    op.execute("""DO $$ BEGIN
      IF EXISTS(SELECT 1 FROM knowledge_document_versions WHERE embedding_provider IS NULL OR embedding_model IS NULL
        OR embedding_model_version IS NULL OR embedding_dimensions IS NULL) THEN
        RAISE EXCEPTION 'KNOWLEDGE_UNINDEXED_SOURCE_EXISTS: bind model metadata before downgrade';
      END IF;
    END $$""")
    for column in ("embedding_provider","embedding_model","embedding_model_version","embedding_dimensions"):
        op.execute(f"ALTER TABLE knowledge_document_versions ALTER COLUMN {column} SET NOT NULL")
    op.execute("DROP INDEX uq_knowledge_upload_request")
    for column in ("request_hash","client_request_id","enabled"):
        op.execute(f"ALTER TABLE knowledge_documents DROP COLUMN {column}")
    # 旧代码没有enabled守卫；降级前必须停止知识查询，避免重新开放曾禁用的ready资料。
