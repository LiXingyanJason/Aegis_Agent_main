"""Shadow index generations only; never changes the production retrieval pointer."""
from alembic import op
from migrations.sql_runner import split_sql_statements

revision = '023_shadow_index_generations'
down_revision = '022_skills'
branch_labels = depends_on = None

DDL = """
CREATE TABLE knowledge_index_generations (
 id uuid PRIMARY KEY, tenant_id uuid NOT NULL, user_id uuid NOT NULL,
 document_id uuid NOT NULL, version_id uuid NOT NULL,
 request_id varchar(128) NOT NULL, request_hash char(64) NOT NULL,
 source_hash char(64) NOT NULL, parser_version varchar(64) NOT NULL,
 config jsonb NOT NULL, manifest jsonb NOT NULL, manifest_hash char(64) NOT NULL,
 status varchar(16) NOT NULL DEFAULT 'prepared' CHECK(status IN ('prepared','validated')),
 validation_hash char(64), validation_reference varchar(200),
 created_at timestamptz NOT NULL DEFAULT now(),
 FOREIGN KEY(version_id,document_id,tenant_id) REFERENCES knowledge_document_versions(id,document_id,tenant_id),
 UNIQUE(tenant_id,user_id,request_id), UNIQUE(id,tenant_id,user_id,document_id,version_id),
 CHECK(status<>'validated' OR (validation_hash=manifest_hash AND validation_reference IS NOT NULL))
);
CREATE TABLE knowledge_generation_chunks (
 generation_id uuid NOT NULL, tenant_id uuid NOT NULL, user_id uuid NOT NULL,
 document_id uuid NOT NULL, version_id uuid NOT NULL,
 chunk_id uuid NOT NULL, position integer NOT NULL CHECK(position>=0),
 payload jsonb NOT NULL, PRIMARY KEY(generation_id,chunk_id), UNIQUE(generation_id,position),
 FOREIGN KEY(generation_id,tenant_id,user_id,document_id,version_id)
 REFERENCES knowledge_index_generations(id,tenant_id,user_id,document_id,version_id)
);
CREATE TABLE knowledge_generation_heads (
 tenant_id uuid NOT NULL,user_id uuid NOT NULL,document_id uuid NOT NULL,version_id uuid NOT NULL,
 revision integer NOT NULL DEFAULT 0 CHECK(revision>=0), selected_generation_id uuid,
 PRIMARY KEY(tenant_id,user_id,document_id,version_id),
 FOREIGN KEY(version_id,document_id,tenant_id) REFERENCES knowledge_document_versions(id,document_id,tenant_id),
 FOREIGN KEY(selected_generation_id,tenant_id,user_id,document_id,version_id)
 REFERENCES knowledge_index_generations(id,tenant_id,user_id,document_id,version_id)
);
CREATE TABLE knowledge_generation_events (
 id uuid PRIMARY KEY,tenant_id uuid NOT NULL,user_id uuid NOT NULL,
 document_id uuid NOT NULL,version_id uuid NOT NULL,revision integer NOT NULL,
 from_generation_id uuid,to_generation_id uuid,action varchar(16) NOT NULL CHECK(action IN ('select','rollback')),
 created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(tenant_id,user_id,document_id,version_id,revision),
 FOREIGN KEY(from_generation_id,tenant_id,user_id,document_id,version_id)
 REFERENCES knowledge_index_generations(id,tenant_id,user_id,document_id,version_id),
 FOREIGN KEY(to_generation_id,tenant_id,user_id,document_id,version_id)
 REFERENCES knowledge_index_generations(id,tenant_id,user_id,document_id,version_id)
);
CREATE FUNCTION aegis_shadow_manifest_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP='DELETE' THEN RAISE EXCEPTION 'Shadow index history cannot be deleted'; END IF;
 IF TG_TABLE_NAME='knowledge_index_generations' THEN
   IF (to_jsonb(NEW)-'status'-'validation_hash'-'validation_reference') IS DISTINCT FROM
      (to_jsonb(OLD)-'status'-'validation_hash'-'validation_reference')
      OR OLD.status='validated' THEN
     RAISE EXCEPTION 'Shadow manifest is immutable';
   END IF;
   RETURN NEW;
 END IF;
 RAISE EXCEPTION 'Shadow index history is immutable';
END $$;
CREATE TRIGGER immutable_shadow_generation BEFORE UPDATE OR DELETE ON knowledge_index_generations
 FOR EACH ROW EXECUTE FUNCTION aegis_shadow_manifest_immutable();
CREATE FUNCTION aegis_shadow_chunk_insert() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF NOT EXISTS(SELECT 1 FROM knowledge_index_generations g WHERE g.id=NEW.generation_id
   AND g.tenant_id=NEW.tenant_id AND g.user_id=NEW.user_id AND g.status='prepared' FOR UPDATE) THEN
   RAISE EXCEPTION 'Validated shadow chunks cannot be appended';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER guard_shadow_chunk_insert BEFORE INSERT ON knowledge_generation_chunks
 FOR EACH ROW EXECUTE FUNCTION aegis_shadow_chunk_insert();
CREATE TRIGGER immutable_shadow_chunk BEFORE UPDATE OR DELETE ON knowledge_generation_chunks
 FOR EACH ROW EXECUTE FUNCTION aegis_shadow_manifest_immutable();
CREATE TRIGGER immutable_shadow_event BEFORE UPDATE OR DELETE ON knowledge_generation_events
 FOR EACH ROW EXECUTE FUNCTION aegis_shadow_manifest_immutable();
"""


def upgrade():
    for statement in split_sql_statements(DDL): op.execute(statement)
    for table in ('knowledge_index_generations','knowledge_generation_chunks','knowledge_generation_heads','knowledge_generation_events'):
        op.execute(f'ALTER TABLE {table} ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE {table} FORCE ROW LEVEL SECURITY')
        owns=f'''tenant_id=aegis_current_tenant_id() AND user_id=aegis_current_user_id()
          AND EXISTS(SELECT 1 FROM knowledge_documents d WHERE d.id={table}.document_id
            AND d.tenant_id={table}.tenant_id AND d.user_id=aegis_current_user_id() AND d.deleted_at IS NULL)'''
        op.execute(f'CREATE POLICY {table}_owner ON {table} USING ({owns}) WITH CHECK ({owns})')


def downgrade():
    op.execute("""DO $$ BEGIN IF EXISTS(SELECT 1 FROM knowledge_index_generations) THEN
      RAISE EXCEPTION 'Export and explicitly retire shadow generations before schema downgrade'; END IF; END $$""")
    for table in ('knowledge_generation_events','knowledge_generation_heads','knowledge_generation_chunks','knowledge_index_generations'):
        op.execute(f'DROP TABLE {table}')
    op.execute('DROP FUNCTION aegis_shadow_manifest_immutable()')
    op.execute('DROP FUNCTION aegis_shadow_chunk_insert()')
