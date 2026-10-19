"""手动长期摘要/实体生命周期；旧记录显式映射，追加无正文审计。"""
from alembic import op
from migrations.sql_runner import split_sql_statements

revision='017_memory_manual_lifecycle'
down_revision='016_knowledge_category_default'
branch_labels=depends_on=None

DDL="""
ALTER TABLE memory_items
 ADD COLUMN memory_type varchar(32) NOT NULL DEFAULT 'long_term_summary',
 ADD COLUMN scope varchar(16) NOT NULL DEFAULT 'user',
 ADD COLUMN entity_key varchar(32), ADD COLUMN entity_value jsonb,
 ADD COLUMN sensitivity varchar(16) NOT NULL DEFAULT 'normal',
 ADD COLUMN status varchar(16) NOT NULL DEFAULT 'pending',
 ADD COLUMN version integer NOT NULL DEFAULT 1,
 ADD COLUMN confirmed_at timestamptz, ADD COLUMN expires_at timestamptz,
 ADD COLUMN source_kind varchar(32) NOT NULL DEFAULT 'manual',
 ADD COLUMN source_object_id uuid, ADD COLUMN source_version varchar(128),
 ADD COLUMN supersedes_id uuid REFERENCES memory_items(id);

UPDATE memory_items SET sensitivity=CASE WHEN is_sensitive THEN 'sensitive' ELSE 'normal' END,
 status=CASE WHEN deleted_at IS NULL THEN 'confirmed' ELSE 'deleted' END,
 confirmed_at=CASE WHEN deleted_at IS NULL THEN created_at ELSE NULL END,
 source_kind=CASE WHEN source_run_id IS NULL THEN 'manual' ELSE 'run' END,
 source_object_id=source_run_id;
UPDATE memory_items m SET source_version=md5(ar.id::text||':'||ar.status||':'||extract(epoch FROM ar.updated_at)::text)
 FROM agent_runs ar WHERE ar.id=m.source_run_id AND ar.tenant_id=m.tenant_id AND ar.user_id=m.user_id;
UPDATE memory_items SET status='revoked',confirmed_at=NULL
 WHERE deleted_at IS NULL AND source_kind='run' AND source_version IS NULL;

ALTER TABLE memory_items ADD CONSTRAINT ck_memory_lifecycle CHECK (
 memory_type IN ('long_term_summary','entity') AND scope='user' AND version>0
 AND sensitivity IN ('normal','sensitive') AND is_sensitive=(sensitivity='sensitive')
 AND status IN ('pending','confirmed','revoked','superseded','deleted')
 AND (status<>'confirmed' OR confirmed_at IS NOT NULL)
 AND (status<>'pending' OR confirmed_at IS NULL)
 AND ((deleted_at IS NULL AND status<>'deleted') OR (deleted_at IS NOT NULL AND status='deleted'))
 AND source_kind IN ('manual','run','knowledge_document')
 AND ((source_kind='manual' AND source_object_id IS NULL AND source_version IS NULL AND source_run_id IS NULL)
      OR (source_kind='run' AND source_object_id IS NOT NULL AND source_run_id IS NOT NULL
          AND source_object_id=source_run_id AND (status<>'confirmed' OR source_version IS NOT NULL))
      OR (source_kind='knowledge_document' AND source_object_id IS NOT NULL AND source_version IS NOT NULL AND source_run_id IS NULL))
 AND ((memory_type='long_term_summary' AND entity_key IS NULL AND entity_value IS NULL)
      OR (memory_type='entity' AND entity_key IS NOT NULL AND entity_value IS NOT NULL
          AND entity_key IN ('timezone','contact','reply_style') AND jsonb_typeof(entity_value)='object'
          AND entity_value->>'key' IS NOT NULL AND entity_value->>'key'=entity_key))
);
CREATE UNIQUE INDEX uq_memory_confirmed_entity ON memory_items(tenant_id,user_id,scope,entity_key)
 WHERE status='confirmed' AND deleted_at IS NULL AND entity_key IS NOT NULL;

CREATE FUNCTION aegis_memory_source_accessible(t uuid,u uuid,k text,o uuid,v text)
RETURNS boolean LANGUAGE sql STABLE SECURITY INVOKER AS $$
 SELECT CASE WHEN k='manual' THEN o IS NULL AND v IS NULL
 WHEN k='run' THEN EXISTS (SELECT 1 FROM agent_runs ar WHERE ar.id=o AND ar.tenant_id=t AND ar.user_id=u
   AND ar.status='completed' AND md5(ar.id::text||':'||ar.status||':'||extract(epoch FROM ar.updated_at)::text)=v
   AND (ar.conversation_id IS NULL OR EXISTS (SELECT 1 FROM conversations c WHERE c.id=ar.conversation_id
     AND c.tenant_id=t AND c.user_id=u AND c.status='active')))
 WHEN k='knowledge_document' THEN EXISTS (SELECT 1 FROM knowledge_documents d JOIN knowledge_document_versions dv
   ON dv.id=d.current_version_id AND dv.tenant_id=d.tenant_id AND dv.document_id=d.id
   WHERE d.id=o AND d.tenant_id=t AND (d.user_id=u OR d.visibility='tenant') AND d.deleted_at IS NULL
     AND d.enabled AND NOT d.is_sensitive AND d.external_embedding_allowed AND dv.status='ready' AND dv.id::text=v)
 ELSE false END
$$;
CREATE TABLE memory_item_revisions (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES tenants(id),
 user_id uuid NOT NULL REFERENCES users(id), memory_id uuid NOT NULL REFERENCES memory_items(id),
 memory_version integer NOT NULL, event_type varchar(32) NOT NULL,
 status varchar(16) NOT NULL, sensitivity varchar(16) NOT NULL,
 memory_type varchar(32) NOT NULL, entity_key varchar(32),
 content_hash char(32) NOT NULL, source_kind varchar(32) NOT NULL, source_version varchar(128),
 supersedes_id uuid, created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE memory_item_revisions ENABLE ROW LEVEL SECURITY;
CREATE POLICY memory_revision_owner ON memory_item_revisions
 USING(tenant_id=aegis_current_tenant_id() AND user_id=aegis_current_user_id())
 WITH CHECK(tenant_id=aegis_current_tenant_id() AND user_id=aegis_current_user_id());
INSERT INTO memory_item_revisions(tenant_id,user_id,memory_id,memory_version,event_type,status,sensitivity,memory_type,entity_key,
 content_hash,source_kind,source_version,supersedes_id)
 SELECT tenant_id,user_id,id,version,'legacy_migrated',status,sensitivity,memory_type,entity_key,
 md5(content||coalesce(entity_value::text,'')),source_kind,source_version,supersedes_id
 FROM memory_items;
CREATE FUNCTION aegis_audit_memory_revision() RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER AS $$
BEGIN
 INSERT INTO memory_item_revisions(tenant_id,user_id,memory_id,memory_version,event_type,status,sensitivity,memory_type,entity_key,
   content_hash,source_kind,source_version,supersedes_id)
 VALUES(NEW.tenant_id,NEW.user_id,NEW.id,NEW.version,
   CASE WHEN TG_OP='INSERT' THEN 'manual_created' WHEN NEW.status<>OLD.status THEN NEW.status ELSE 'edited' END,
   NEW.status,NEW.sensitivity,NEW.memory_type,NEW.entity_key,md5(NEW.content||coalesce(NEW.entity_value::text,'')),NEW.source_kind,NEW.source_version,NEW.supersedes_id);
 RETURN NEW;
END $$;
CREATE TRIGGER trg_memory_revision AFTER INSERT OR UPDATE ON memory_items
 FOR EACH ROW EXECUTE FUNCTION aegis_audit_memory_revision();
"""


def upgrade():
    for statement in split_sql_statements(DDL): op.execute(statement)


def downgrade():
    # 旧Worker不理解pending/实体/来源撤权；拒绝丢弃这些语义后让旧值意外进入Prompt。
    op.execute("""DO $$ BEGIN IF EXISTS (SELECT 1 FROM memory_items WHERE deleted_at IS NULL
      AND (status<>'confirmed' OR memory_type<>'long_term_summary' OR expires_at IS NOT NULL OR source_kind<>'manual'))
      THEN RAISE EXCEPTION 'Cannot downgrade memory lifecycle with new-format active records; export and resolve manually first';
      END IF; END $$""")
    op.execute('DROP TRIGGER trg_memory_revision ON memory_items')
    op.execute('DROP FUNCTION aegis_audit_memory_revision()')
    op.execute('DROP TABLE memory_item_revisions')
    op.execute('DROP FUNCTION aegis_memory_source_accessible(uuid,uuid,text,uuid,text)')
    op.execute('DROP INDEX uq_memory_confirmed_entity')
    op.execute('ALTER TABLE memory_items DROP CONSTRAINT ck_memory_lifecycle')
    for name in ('supersedes_id','source_version','source_object_id','source_kind','expires_at','confirmed_at',
                 'version','status','sensitivity','entity_value','entity_key','scope','memory_type'):
        op.execute('ALTER TABLE memory_items DROP COLUMN '+name)
