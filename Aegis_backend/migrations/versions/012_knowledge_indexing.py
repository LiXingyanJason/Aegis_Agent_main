"""本地源文件引用、幂等导入与可恢复索引；不建设多版本更新。"""
from alembic import op
from migrations.sql_runner import split_sql_statements

revision = "012_knowledge_indexing"
down_revision = "011_embedding_governance"
branch_labels = depends_on = None


def upgrade():
    ddl = """
    ALTER TABLE knowledge_document_versions ALTER COLUMN source_text DROP NOT NULL;
    ALTER TABLE knowledge_document_versions ADD COLUMN storage_backend varchar(16);
    ALTER TABLE knowledge_document_versions ADD COLUMN storage_key varchar(128);
    ALTER TABLE knowledge_document_versions ADD COLUMN original_filename varchar(255);
    ALTER TABLE knowledge_document_versions ADD COLUMN source_bytes bigint;
    ALTER TABLE knowledge_document_versions ADD COLUMN source_mime varchar(64);
    ALTER TABLE knowledge_document_versions ADD CONSTRAINT ck_knowledge_source_location CHECK
      (source_text IS NOT NULL OR (storage_backend IS NOT NULL AND storage_backend='local' AND storage_key IS NOT NULL
        AND source_bytes IS NOT NULL AND source_bytes>0 AND source_mime IS NOT NULL));
    ALTER TABLE knowledge_index_jobs ADD COLUMN client_request_id varchar(128);
    ALTER TABLE knowledge_index_jobs ADD COLUMN request_hash char(64);
    ALTER TABLE knowledge_index_jobs ADD COLUMN lease_token uuid;
    ALTER TABLE knowledge_index_jobs ADD COLUMN lease_expires_at timestamptz;
    ALTER TABLE knowledge_index_jobs ADD COLUMN last_dispatched_at timestamptz;
    ALTER TABLE knowledge_index_jobs ADD COLUMN total_chunks integer NOT NULL DEFAULT 0 CHECK(total_chunks>=0);
    ALTER TABLE knowledge_index_jobs ADD COLUMN processed_chunks integer NOT NULL DEFAULT 0 CHECK(processed_chunks>=0);
    ALTER TABLE knowledge_index_jobs ADD COLUMN vector_collection varchar(128);
    ALTER TABLE knowledge_index_jobs ADD COLUMN vector_namespace char(64);
    ALTER TABLE knowledge_index_jobs ADD COLUMN parser_version varchar(32);
    ALTER TABLE knowledge_chunks ADD COLUMN indexed_at timestamptz;
    ALTER TABLE knowledge_chunks ADD COLUMN search_tokens text;
    ALTER TABLE knowledge_chunks DROP COLUMN search_text;
    ALTER TABLE knowledge_chunks ADD COLUMN search_text tsvector GENERATED ALWAYS AS
      (to_tsvector('simple',coalesce(search_tokens,content))) STORED;
    CREATE INDEX ix_knowledge_chunks_fts ON knowledge_chunks USING gin(search_text);
    CREATE UNIQUE INDEX uq_knowledge_import_request ON knowledge_index_jobs(tenant_id,user_id,client_request_id)
      WHERE client_request_id IS NOT NULL;
    CREATE INDEX ix_knowledge_job_recovery ON knowledge_index_jobs(status,lease_expires_at,last_dispatched_at);
    CREATE TABLE knowledge_vector_spaces (
      namespace char(64) PRIMARY KEY,collection_name varchar(128) NOT NULL,
      model_provider varchar(64) NOT NULL,model_name varchar(128) NOT NULL,
      model_version varchar(64) NOT NULL,dimensions integer NOT NULL CHECK(dimensions BETWEEN 1 AND 2000),
      created_at timestamptz NOT NULL DEFAULT now()
    );
    """
    for statement in split_sql_statements(ddl):
        op.execute(statement)


def downgrade():
    # 外置源文件不能凭空恢复成旧内联文本；有此类记录时拒绝降级，不删除资料。
    op.execute("""DO $$ BEGIN
      IF EXISTS(SELECT 1 FROM knowledge_document_versions WHERE source_text IS NULL) THEN
        RAISE EXCEPTION 'KNOWLEDGE_EXTERNAL_SOURCE_EXISTS: restore inline source_text before downgrade';
      END IF;
    END $$""")
    op.execute("DROP INDEX ix_knowledge_job_recovery")
    op.execute("DROP TABLE knowledge_vector_spaces")
    op.execute("DROP INDEX uq_knowledge_import_request")
    op.execute("ALTER TABLE knowledge_chunks DROP COLUMN search_text")
    op.execute("ALTER TABLE knowledge_chunks DROP COLUMN search_tokens")
    op.execute("ALTER TABLE knowledge_chunks DROP COLUMN indexed_at")
    op.execute("ALTER TABLE knowledge_chunks ADD COLUMN search_text tsvector GENERATED ALWAYS AS (to_tsvector('simple',content)) STORED")
    op.execute("CREATE INDEX ix_knowledge_chunks_fts ON knowledge_chunks USING gin(search_text)")
    for column in ("client_request_id", "request_hash", "lease_token", "lease_expires_at", "last_dispatched_at",
                   "total_chunks", "processed_chunks", "vector_collection", "vector_namespace", "parser_version"):
        op.execute(f"ALTER TABLE knowledge_index_jobs DROP COLUMN {column}")
    op.execute("ALTER TABLE knowledge_document_versions DROP CONSTRAINT ck_knowledge_source_location")
    for column in ("storage_backend", "storage_key", "original_filename", "source_bytes", "source_mime"):
        op.execute(f"ALTER TABLE knowledge_document_versions DROP COLUMN {column}")
    op.execute("ALTER TABLE knowledge_document_versions ALTER COLUMN source_text SET NOT NULL")
