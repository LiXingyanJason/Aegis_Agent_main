"""Freeze start-index strategy; shared atomic publication guard, no auto publication."""
from alembic import op
from migrations.sql_runner import split_sql_statements
revision='026_automatic_indexing'
down_revision='025_generation_runtime'
branch_labels=depends_on=None

DDL='''
ALTER TABLE knowledge_index_jobs ADD COLUMN index_config jsonb;
ALTER TABLE knowledge_index_jobs ADD COLUMN generation_id uuid REFERENCES knowledge_index_generations(id);
CREATE UNIQUE INDEX uq_auto_generation_job ON knowledge_index_jobs(generation_id) WHERE generation_id IS NOT NULL;
CREATE FUNCTION aegis_index_config_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF NEW.index_config IS DISTINCT FROM OLD.index_config OR
    (OLD.generation_id IS NOT NULL AND NEW.generation_id IS DISTINCT FROM OLD.generation_id) THEN
   RAISE EXCEPTION 'Index job strategy and generation binding are immutable';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER immutable_index_job_strategy BEFORE UPDATE ON knowledge_index_jobs
 FOR EACH ROW EXECUTE FUNCTION aegis_index_config_immutable();
CREATE FUNCTION aegis_publish_generation(p_tenant uuid,p_user uuid,p_doc uuid,p_version uuid,
 p_target uuid,p_expected integer,p_review text,p_rollback boolean DEFAULT false)
 RETURNS TABLE(generation_id uuid,revision integer,changed boolean) LANGUAGE plpgsql AS $$
DECLARE v_revision integer; v_old uuid; v_hash text; v_title text;
BEGIN
 IF p_tenant IS DISTINCT FROM aegis_current_tenant_id() OR p_user IS DISTINCT FROM aegis_current_user_id()
    OR p_expected IS NULL OR p_expected<0 OR p_review IS NULL OR length(btrim(p_review)) NOT BETWEEN 1 AND 200 THEN
   RAISE EXCEPTION 'GENERATION_PUBLICATION_REJECTED';
 END IF;
 SELECT v.content_hash,d.title INTO v_hash,v_title FROM knowledge_documents d
 JOIN knowledge_document_versions v ON v.id=d.current_version_id AND v.document_id=d.id AND v.tenant_id=d.tenant_id
 WHERE d.id=p_doc AND d.tenant_id=p_tenant AND d.user_id=p_user AND d.deleted_at IS NULL
 AND NOT d.is_sensitive AND d.external_embedding_allowed AND v.id=p_version AND v.status='ready' FOR UPDATE OF d;
 IF NOT FOUND THEN RAISE EXCEPTION 'SHADOW_SOURCE_UNAVAILABLE'; END IF;
 IF p_target IS NULL AND NOT p_rollback THEN RAISE EXCEPTION 'GENERATION_NOT_READY'; END IF;
 IF p_target IS NOT NULL THEN
   PERFORM 1 FROM knowledge_generation_builds b JOIN knowledge_index_generations g ON g.id=b.generation_id AND g.tenant_id=b.tenant_id
   JOIN knowledge_document_versions v ON v.id=b.version_id AND v.document_id=b.document_id AND v.tenant_id=b.tenant_id
   WHERE b.generation_id=p_target AND b.tenant_id=p_tenant AND b.user_id=p_user AND b.document_id=p_doc AND b.version_id=p_version
   AND b.status='ready' AND g.status='validated' AND g.source_hash=v_hash
   AND (NOT (g.config->>'title_enhancement')::boolean OR g.manifest->>'source_title'=v_title)
   AND b.namespace=v.vector_namespace AND b.collection=v.vector_collection
   AND (SELECT count(*) FROM knowledge_generation_points x WHERE x.generation_id=b.generation_id)>0
   AND (SELECT count(*) FROM knowledge_generation_points x WHERE x.generation_id=b.generation_id)=
       (SELECT count(*) FROM knowledge_generation_chunks c WHERE c.generation_id=b.generation_id);
   IF NOT FOUND THEN RAISE EXCEPTION 'GENERATION_NOT_READY'; END IF;
   IF p_rollback THEN
     PERFORM 1 FROM knowledge_generation_releases r WHERE r.tenant_id=p_tenant AND r.document_id=p_doc
       AND r.version_id=p_version AND r.generation_id=p_target;
     IF NOT FOUND THEN RAISE EXCEPTION 'GENERATION_NOT_PREVIOUS'; END IF;
   END IF;
 END IF;
 INSERT INTO knowledge_generation_publications(tenant_id,user_id,document_id,version_id)
 VALUES(p_tenant,p_user,p_doc,p_version) ON CONFLICT DO NOTHING;
 SELECT h.revision,h.generation_id INTO v_revision,v_old FROM knowledge_generation_publications h
 WHERE h.tenant_id=p_tenant AND h.user_id=p_user AND h.document_id=p_doc AND h.version_id=p_version FOR UPDATE;
 IF NOT FOUND OR v_revision<>p_expected THEN RAISE EXCEPTION 'GENERATION_REVISION_CONFLICT'; END IF;
 IF v_old IS NOT DISTINCT FROM p_target THEN RETURN QUERY SELECT p_target,v_revision,false; RETURN; END IF;
 UPDATE knowledge_generation_publications h SET generation_id=p_target,revision=v_revision+1
 WHERE h.tenant_id=p_tenant AND h.user_id=p_user AND h.document_id=p_doc AND h.version_id=p_version;
 INSERT INTO knowledge_generation_releases(tenant_id,user_id,document_id,version_id,revision,old_generation_id,generation_id,review_reference)
 VALUES(p_tenant,p_user,p_doc,p_version,v_revision+1,v_old,p_target,p_review);
 RETURN QUERY SELECT p_target,v_revision+1,true;
END $$;
'''


def upgrade():
    for s in split_sql_statements(DDL):op.execute(s)


def downgrade():
    op.execute("DO $$ BEGIN IF EXISTS(SELECT 1 FROM knowledge_index_jobs WHERE index_config IS NOT NULL OR generation_id IS NOT NULL) THEN RAISE EXCEPTION 'Preserve automatic jobs before downgrade'; END IF; END $$")
    op.execute('DROP FUNCTION aegis_publish_generation(uuid,uuid,uuid,uuid,uuid,integer,text,boolean)')
    op.execute('DROP TRIGGER immutable_index_job_strategy ON knowledge_index_jobs')
    op.execute('DROP FUNCTION aegis_index_config_immutable()')
    op.execute('DROP INDEX uq_auto_generation_job')
    op.execute('ALTER TABLE knowledge_index_jobs DROP COLUMN generation_id')
    op.execute('ALTER TABLE knowledge_index_jobs DROP COLUMN index_config')
