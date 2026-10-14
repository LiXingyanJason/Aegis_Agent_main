"""知识文档、版本、片段及索引任务；权限来自文档归属而非内容标签。"""
from alembic import op
from migrations.sql_runner import split_sql_statements

revision = "010_knowledge_schema"
down_revision = "009_model_usage_audit"
branch_labels = depends_on = None

DDL = """
CREATE TABLE knowledge_documents (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES tenants(id),
 user_id uuid NOT NULL REFERENCES users(id), title varchar(200) NOT NULL CHECK(length(btrim(title))>0),
 category varchar(32) NOT NULL CHECK(category IN ('project','policy','contract','minutes')),
 visibility varchar(16) NOT NULL DEFAULT 'private' CHECK(visibility IN ('private','tenant')),
 is_sensitive boolean NOT NULL DEFAULT false,
 external_embedding_allowed boolean NOT NULL DEFAULT false,
 deleted_at timestamptz, created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
 current_version_id uuid, UNIQUE(id,tenant_id)
);
CREATE TABLE knowledge_document_versions (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), document_id uuid NOT NULL, tenant_id uuid NOT NULL,
 version_no integer NOT NULL CHECK(version_no>0), version_label varchar(64) NOT NULL,
 source_text text NOT NULL CHECK(length(source_text)>0), content_hash char(64) NOT NULL CHECK(content_hash ~ '^[0-9a-f]{64}$'),
 status varchar(16) NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','indexing','ready','failed','retired')),
 embedding_provider varchar(64) NOT NULL, embedding_model varchar(128) NOT NULL,
 embedding_model_version varchar(64) NOT NULL, embedding_dimensions integer NOT NULL CHECK(embedding_dimensions BETWEEN 1 AND 2000),
 created_at timestamptz NOT NULL DEFAULT now(),
 FOREIGN KEY(document_id,tenant_id) REFERENCES knowledge_documents(id,tenant_id) ON DELETE CASCADE,
 UNIQUE(document_id,version_no), UNIQUE(id,document_id,tenant_id),
 UNIQUE(id,document_id,tenant_id,embedding_provider,embedding_model,embedding_model_version,embedding_dimensions)
);
ALTER TABLE knowledge_documents ADD CONSTRAINT fk_knowledge_current_version
 FOREIGN KEY(current_version_id,id,tenant_id) REFERENCES knowledge_document_versions(id,document_id,tenant_id) DEFERRABLE INITIALLY DEFERRED;
CREATE TABLE knowledge_chunks (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), document_id uuid NOT NULL, version_id uuid NOT NULL, tenant_id uuid NOT NULL,
 position integer NOT NULL CHECK(position>=0), section varchar(200), content text NOT NULL CHECK(length(content)>0),
 start_offset integer NOT NULL CHECK(start_offset>=0), end_offset integer NOT NULL CHECK(end_offset>start_offset),
 content_hash char(64) NOT NULL CHECK(content_hash ~ '^[0-9a-f]{64}$'),
 embedding_provider varchar(64) NOT NULL, embedding_model varchar(128) NOT NULL,
 embedding_model_version varchar(64) NOT NULL, embedding_dimensions integer NOT NULL CHECK(embedding_dimensions BETWEEN 1 AND 2000),
 search_text tsvector GENERATED ALWAYS AS (to_tsvector('simple',content)) STORED,
 FOREIGN KEY(version_id,document_id,tenant_id,embedding_provider,embedding_model,embedding_model_version,embedding_dimensions)
 REFERENCES knowledge_document_versions(id,document_id,tenant_id,embedding_provider,embedding_model,embedding_model_version,embedding_dimensions) ON DELETE CASCADE,
 UNIQUE(version_id,position)
);
CREATE TABLE knowledge_index_jobs (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL, user_id uuid NOT NULL REFERENCES users(id),
 document_id uuid NOT NULL, version_id uuid NOT NULL,
 status varchar(16) NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','running','succeeded','failed','cancelled')),
 attempt_count integer NOT NULL DEFAULT 0 CHECK(attempt_count>=0), error_code varchar(64),
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
 FOREIGN KEY(version_id,document_id,tenant_id) REFERENCES knowledge_document_versions(id,document_id,tenant_id) ON DELETE CASCADE,
 UNIQUE(version_id)
);
CREATE INDEX ix_knowledge_documents_owner ON knowledge_documents(tenant_id,user_id,visibility) WHERE deleted_at IS NULL;
CREATE INDEX ix_knowledge_chunks_version ON knowledge_chunks(tenant_id,document_id,version_id);
CREATE INDEX ix_knowledge_chunks_fts ON knowledge_chunks USING gin(search_text);
CREATE INDEX ix_knowledge_jobs_queue ON knowledge_index_jobs(tenant_id,user_id,status);
"""


def upgrade():
    for statement in split_sql_statements(DDL):
        op.execute(statement)
    for table in ("knowledge_documents", "knowledge_document_versions", "knowledge_chunks", "knowledge_index_jobs"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    owner = "tenant_id=aegis_current_tenant_id() AND user_id=aegis_current_user_id()"
    readable = "tenant_id=aegis_current_tenant_id() AND (user_id=aegis_current_user_id() OR visibility='tenant') AND deleted_at IS NULL"
    op.execute(f"CREATE POLICY knowledge_document_read ON knowledge_documents FOR SELECT USING ({readable})")
    op.execute(f"CREATE POLICY knowledge_document_insert ON knowledge_documents FOR INSERT WITH CHECK ({owner} AND visibility='private' AND EXISTS(SELECT 1 FROM users u WHERE u.id=knowledge_documents.user_id AND u.tenant_id=knowledge_documents.tenant_id))")
    op.execute(f"CREATE POLICY knowledge_document_update ON knowledge_documents FOR UPDATE USING ({owner} AND deleted_at IS NULL) WITH CHECK ({owner})")
    for table in ("knowledge_document_versions", "knowledge_chunks"):
        exists = f"EXISTS(SELECT 1 FROM knowledge_documents d WHERE d.id={table}.document_id AND d.tenant_id={table}.tenant_id)"
        owns = f"EXISTS(SELECT 1 FROM knowledge_documents d WHERE d.id={table}.document_id AND d.tenant_id={table}.tenant_id AND d.user_id=aegis_current_user_id())"
        op.execute(f"CREATE POLICY {table}_read ON {table} FOR SELECT USING (tenant_id=aegis_current_tenant_id() AND {exists})")
        op.execute(f"CREATE POLICY {table}_write ON {table} FOR ALL USING (tenant_id=aegis_current_tenant_id() AND {owns}) WITH CHECK (tenant_id=aegis_current_tenant_id() AND {owns})")
    op.execute(f"CREATE POLICY knowledge_jobs_owner ON knowledge_index_jobs USING ({owner} AND EXISTS(SELECT 1 FROM knowledge_documents d WHERE d.id=knowledge_index_jobs.document_id AND d.tenant_id=knowledge_index_jobs.tenant_id AND d.user_id=aegis_current_user_id())) WITH CHECK ({owner} AND EXISTS(SELECT 1 FROM knowledge_documents d WHERE d.id=knowledge_index_jobs.document_id AND d.tenant_id=knowledge_index_jobs.tenant_id AND d.user_id=aegis_current_user_id()))")


def downgrade():
    op.execute("ALTER TABLE knowledge_documents DROP CONSTRAINT fk_knowledge_current_version")
    for table in ("knowledge_index_jobs", "knowledge_chunks", "knowledge_document_versions", "knowledge_documents"):
        op.execute(f"DROP TABLE {table}")
