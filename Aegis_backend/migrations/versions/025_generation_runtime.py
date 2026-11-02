"""Generation build leases, governed embedding subjects and opt-in publication."""
from alembic import op
from migrations.sql_runner import split_sql_statements
revision='025_generation_runtime'
down_revision='024_rerank_governance'
branch_labels=depends_on=None

DDL='''
CREATE TABLE knowledge_generation_builds (
 generation_id uuid PRIMARY KEY,tenant_id uuid NOT NULL,user_id uuid NOT NULL,
 document_id uuid NOT NULL,version_id uuid NOT NULL,
 status varchar(16) NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','running','ready','failed')),
 lease_token uuid,lease_expires_at timestamptz,attempts integer NOT NULL DEFAULT 0,
 namespace varchar(64),collection varchar(128),error_code varchar(64),
 FOREIGN KEY(generation_id,tenant_id,user_id,document_id,version_id)
 REFERENCES knowledge_index_generations(id,tenant_id,user_id,document_id,version_id)
);
CREATE TABLE knowledge_generation_points (
 generation_id uuid NOT NULL,chunk_id uuid NOT NULL,indexed_at timestamptz NOT NULL DEFAULT now(),
 tenant_id uuid NOT NULL,user_id uuid NOT NULL,
 PRIMARY KEY(generation_id,chunk_id),FOREIGN KEY(generation_id,chunk_id)
 REFERENCES knowledge_generation_chunks(generation_id,chunk_id)
);
ALTER TABLE knowledge_generation_chunks ADD CONSTRAINT uq_generation_chunk_owner UNIQUE(generation_id,chunk_id,tenant_id,user_id);
ALTER TABLE knowledge_generation_points ADD CONSTRAINT fk_generation_point_owner
 FOREIGN KEY(generation_id,chunk_id,tenant_id,user_id) REFERENCES knowledge_generation_chunks(generation_id,chunk_id,tenant_id,user_id);
CREATE TABLE knowledge_generation_publications (
 tenant_id uuid NOT NULL,user_id uuid NOT NULL,document_id uuid NOT NULL,version_id uuid NOT NULL,
 revision integer NOT NULL DEFAULT 0,generation_id uuid,
 PRIMARY KEY(tenant_id,document_id,version_id),
 FOREIGN KEY(generation_id,tenant_id,user_id,document_id,version_id)
 REFERENCES knowledge_index_generations(id,tenant_id,user_id,document_id,version_id)
);
CREATE TABLE knowledge_generation_releases (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),tenant_id uuid NOT NULL,user_id uuid NOT NULL,
 document_id uuid NOT NULL,version_id uuid NOT NULL,revision integer NOT NULL,
 old_generation_id uuid,generation_id uuid,review_reference varchar(200) NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(),UNIQUE(tenant_id,document_id,version_id,revision)
);
CREATE TRIGGER immutable_generation_release BEFORE UPDATE OR DELETE ON knowledge_generation_releases
 FOR EACH ROW EXECUTE FUNCTION aegis_shadow_manifest_immutable();
ALTER TABLE embedding_execution_budgets ADD COLUMN generation_id uuid UNIQUE REFERENCES knowledge_generation_builds(generation_id);
ALTER TABLE embedding_execution_budgets DROP CONSTRAINT embedding_execution_budgets_check;
ALTER TABLE embedding_execution_budgets ADD CONSTRAINT embedding_execution_budgets_check
 CHECK(num_nonnulls(run_id,index_job_id,generation_id)=1);
CREATE VIEW knowledge_runtime_chunks WITH (security_invoker=true) AS
 SELECT c.id,c.document_id,c.version_id,c.tenant_id,c.position,c.section,c.content,c.start_offset,c.end_offset,
 c.content_hash,c.indexed_at,c.search_text,NULL::uuid AS generation_id
 FROM knowledge_chunks c WHERE NOT EXISTS(SELECT 1 FROM knowledge_generation_publications h
 WHERE h.tenant_id=c.tenant_id AND h.document_id=c.document_id AND h.version_id=c.version_id AND h.generation_id IS NOT NULL)
 UNION ALL
 SELECT c.chunk_id,c.document_id,c.version_id,c.tenant_id,c.position,
 coalesce(nullif(left(array_to_string(ARRAY(SELECT jsonb_array_elements_text(c.payload->'raw'->'heading_path')),' / '),200),''),'正文'),
 c.payload->'raw'->>'content',(c.payload->'raw'->'source_range'->>'start')::integer,
 (c.payload->'raw'->'source_range'->>'end')::integer,c.payload->'raw'->>'content_hash',p.indexed_at,
 to_tsvector('simple',coalesce(c.payload->>'search_tokens',c.payload->>'retrieval_text')),c.generation_id
 FROM knowledge_generation_chunks c JOIN knowledge_generation_points p ON p.generation_id=c.generation_id AND p.chunk_id=c.chunk_id
 JOIN knowledge_generation_builds b ON b.generation_id=c.generation_id AND b.status='ready'
 JOIN knowledge_index_generations g ON g.id=c.generation_id AND g.tenant_id=c.tenant_id
 JOIN knowledge_document_versions pv ON pv.id=c.version_id AND pv.document_id=c.document_id AND pv.tenant_id=c.tenant_id
   AND pv.content_hash=g.source_hash AND pv.vector_namespace=b.namespace AND pv.vector_collection=b.collection
 JOIN knowledge_documents pd ON pd.id=c.document_id AND pd.tenant_id=c.tenant_id
   AND (NOT (g.config->>'title_enhancement')::boolean OR g.manifest->>'source_title'=pd.title)
 JOIN knowledge_generation_publications h ON h.generation_id=c.generation_id AND h.tenant_id=c.tenant_id
 AND h.document_id=c.document_id AND h.version_id=c.version_id;
CREATE VIEW knowledge_historical_chunks WITH (security_invoker=true) AS
 SELECT c.id,c.document_id,c.version_id,c.tenant_id,c.position,c.section,c.content,c.start_offset,c.end_offset,
 c.content_hash,c.indexed_at,NULL::uuid AS generation_id FROM knowledge_chunks c
 UNION ALL
 SELECT c.chunk_id,c.document_id,c.version_id,c.tenant_id,c.position,
 coalesce(nullif(left(array_to_string(ARRAY(SELECT jsonb_array_elements_text(c.payload->'raw'->'heading_path')),' / '),200),''),'正文'),
 c.payload->'raw'->>'content',(c.payload->'raw'->'source_range'->>'start')::integer,
 (c.payload->'raw'->'source_range'->>'end')::integer,c.payload->'raw'->>'content_hash',p.indexed_at,c.generation_id
 FROM knowledge_generation_chunks c JOIN knowledge_generation_points p ON p.generation_id=c.generation_id AND p.chunk_id=c.chunk_id
 JOIN knowledge_generation_builds b ON b.generation_id=c.generation_id AND b.status='ready'
 JOIN knowledge_index_generations g ON g.id=c.generation_id AND g.tenant_id=c.tenant_id
 JOIN knowledge_document_versions pv ON pv.id=c.version_id AND pv.document_id=c.document_id AND pv.tenant_id=c.tenant_id
   AND pv.content_hash=g.source_hash AND pv.vector_namespace=b.namespace AND pv.vector_collection=b.collection;
'''


def upgrade():
    for s in split_sql_statements(DDL):op.execute(s)
    for table in ('knowledge_generation_builds','knowledge_generation_points','knowledge_generation_publications','knowledge_generation_releases'):
        op.execute(f'ALTER TABLE {table} ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE {table} FORCE ROW LEVEL SECURITY')
        # Reads may be tenant-shared; writes still owner-only. Generation chunks
        # gain a SELECT-only policy below; immutable content is never tenant-writable.
        owner='tenant_id=aegis_current_tenant_id() AND user_id=aegis_current_user_id()'
        op.execute(f'CREATE POLICY {table}_owner ON {table} USING ({owner}) WITH CHECK ({owner})')
    for table in ('knowledge_index_generations','knowledge_generation_chunks','knowledge_generation_builds','knowledge_generation_points','knowledge_generation_publications'):
        if table=='knowledge_generation_points':
            doc='EXISTS(SELECT 1 FROM knowledge_generation_chunks c JOIN knowledge_documents d ON d.id=c.document_id AND d.tenant_id=c.tenant_id WHERE c.chunk_id=knowledge_generation_points.chunk_id AND c.generation_id=knowledge_generation_points.generation_id AND d.deleted_at IS NULL)'
        else:
            doc=f'EXISTS(SELECT 1 FROM knowledge_documents d WHERE d.id={table}.document_id AND d.tenant_id={table}.tenant_id AND d.deleted_at IS NULL)'
        op.execute(f'CREATE POLICY {table}_visible ON {table} FOR SELECT USING(tenant_id=aegis_current_tenant_id() AND {doc})')


def downgrade():
    op.execute("DO $$ BEGIN IF EXISTS(SELECT 1 FROM knowledge_generation_builds) THEN RAISE EXCEPTION 'Preserve generation builds and audit; rollback publication first'; END IF; END $$")
    op.execute('DROP VIEW knowledge_runtime_chunks')
    op.execute('DROP VIEW knowledge_historical_chunks')
    for table in ('knowledge_index_generations','knowledge_generation_chunks'):
        op.execute(f'DROP POLICY {table}_visible ON {table}')
    op.execute('ALTER TABLE embedding_execution_budgets DROP CONSTRAINT embedding_execution_budgets_check')
    op.execute('ALTER TABLE embedding_execution_budgets DROP COLUMN generation_id')
    op.execute('ALTER TABLE embedding_execution_budgets ADD CONSTRAINT embedding_execution_budgets_check CHECK((run_id IS NULL)<>(index_job_id IS NULL))')
    for table in ('knowledge_generation_releases','knowledge_generation_publications','knowledge_generation_points','knowledge_generation_builds'):op.execute(f'DROP TABLE {table}')
    op.execute('ALTER TABLE knowledge_generation_chunks DROP CONSTRAINT uq_generation_chunk_owner')
