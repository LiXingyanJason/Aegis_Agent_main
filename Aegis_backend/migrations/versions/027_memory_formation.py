"""Formation settings, jobs and proposals, separate from active memories."""
from alembic import op
from migrations.sql_runner import split_sql_statements
revision='027_memory_formation'
down_revision='026_automatic_indexing'
branch_labels=depends_on=None

DDL='''
CREATE TABLE memory_formation_settings(
 tenant_id uuid NOT NULL REFERENCES tenants(id),user_id uuid NOT NULL REFERENCES users(id),
 version integer NOT NULL DEFAULT 0 CHECK(version>=0),
 mode text NOT NULL DEFAULT 'suggest' CHECK(mode IN ('off','suggest','controlled_auto')),
 use_existing boolean NOT NULL DEFAULT true,external_processing_allowed boolean NOT NULL DEFAULT false,
 auto_approval_consent boolean NOT NULL DEFAULT false,
 allowed_categories jsonb NOT NULL DEFAULT '["preference"]',
 excluded_conversation_ids jsonb NOT NULL DEFAULT '[]',updated_at timestamptz NOT NULL DEFAULT now(),
 CHECK((mode='controlled_auto')=auto_approval_consent),
 CHECK(jsonb_typeof(allowed_categories)='array' AND jsonb_array_length(allowed_categories)<=4),
 CHECK(allowed_categories <@ '["preference","stable_fact","project_context","temporary_fact"]'::jsonb),
 CHECK(jsonb_typeof(excluded_conversation_ids)='array' AND jsonb_array_length(excluded_conversation_ids)<=100),
 PRIMARY KEY(tenant_id,user_id));
CREATE TABLE memory_formation_jobs(
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),tenant_id uuid NOT NULL REFERENCES tenants(id),user_id uuid NOT NULL REFERENCES users(id),
 conversation_id uuid NOT NULL REFERENCES conversations(id),idempotency_key text NOT NULL CHECK(length(idempotency_key) BETWEEN 1 AND 200),
 policy_version text NOT NULL,extractor_version text NOT NULL,settings_version integer NOT NULL CHECK(settings_version>=0),
 source_manifest jsonb NOT NULL CHECK(jsonb_typeof(source_manifest)='array' AND jsonb_array_length(source_manifest) BETWEEN 1 AND 20),
 status text NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','running','succeeded','failed','cancelled')),
 attempt_count integer NOT NULL DEFAULT 0 CHECK(attempt_count>=0),lease_token uuid,lease_expires_at timestamptz,
 error_code text,created_at timestamptz NOT NULL DEFAULT now(),updated_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(tenant_id,user_id,idempotency_key),UNIQUE(id,tenant_id,user_id));
CREATE TABLE memory_formation_proposals(
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),tenant_id uuid NOT NULL REFERENCES tenants(id),user_id uuid NOT NULL REFERENCES users(id),
 job_id uuid NOT NULL,version integer NOT NULL DEFAULT 1 CHECK(version>0),
 category text NOT NULL CHECK(category IN ('preference','stable_fact','project_context','temporary_fact')),
 content text NOT NULL CHECK(length(content) BETWEEN 1 AND 4000),
 status text NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','rejected','expired','applied')),
 proposal jsonb NOT NULL CHECK(jsonb_typeof(proposal)='object'),
 created_at timestamptz NOT NULL DEFAULT now(),updated_at timestamptz NOT NULL DEFAULT now(),
 FOREIGN KEY(job_id,tenant_id,user_id) REFERENCES memory_formation_jobs(id,tenant_id,user_id));
CREATE TABLE memory_lifecycle_events(
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),tenant_id uuid NOT NULL REFERENCES tenants(id),user_id uuid NOT NULL REFERENCES users(id),
 event_type text NOT NULL CHECK(event_type IN ('settings_changed','proposal_created','proposal_rejected','proposal_expired','proposal_applied','memory_invalidated','cleanup_requested','cleanup_completed')),
 object_id uuid NOT NULL,object_version integer NOT NULL CHECK(object_version>0),
 reason text NOT NULL CHECK(length(reason) BETWEEN 1 AND 100),created_at timestamptz NOT NULL DEFAULT now());
ALTER TABLE memory_items ADD COLUMN approval_origin text NOT NULL DEFAULT 'legacy';
ALTER TABLE memory_items ADD CONSTRAINT ck_memory_approval_origin CHECK(approval_origin IN ('legacy','user','policy'));
CREATE FUNCTION aegis_memory_event_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'Memory lifecycle events are immutable'; END $$;
CREATE TRIGGER immutable_memory_lifecycle_event BEFORE UPDATE OR DELETE ON memory_lifecycle_events
 FOR EACH ROW EXECUTE FUNCTION aegis_memory_event_immutable();
CREATE FUNCTION aegis_memory_proposal_transition() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF NEW.status IS DISTINCT FROM OLD.status AND
    (OLD.status<>'pending' OR NEW.status NOT IN ('rejected','expired','applied')) THEN
   RAISE EXCEPTION 'Invalid memory proposal transition';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER valid_memory_proposal_transition BEFORE UPDATE ON memory_formation_proposals
 FOR EACH ROW EXECUTE FUNCTION aegis_memory_proposal_transition();
'''


def upgrade():
    for statement in split_sql_statements(DDL):op.execute(statement)
    for table in ('memory_formation_settings','memory_formation_jobs','memory_formation_proposals','memory_lifecycle_events'):
        op.execute(f'ALTER TABLE {table} ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE {table} FORCE ROW LEVEL SECURITY')
        owns='tenant_id=aegis_current_tenant_id() AND user_id=aegis_current_user_id()'
        if table=='memory_formation_jobs':
            owns+=' AND EXISTS(SELECT 1 FROM conversations c WHERE c.id=memory_formation_jobs.conversation_id AND c.tenant_id=memory_formation_jobs.tenant_id AND c.user_id=memory_formation_jobs.user_id)'
        op.execute(f'CREATE POLICY {table}_owner ON {table} USING ({owns}) WITH CHECK ({owns})')


def downgrade():
    op.execute("DO $$ BEGIN IF EXISTS(SELECT 1 FROM memory_formation_settings) OR EXISTS(SELECT 1 FROM memory_formation_jobs) OR EXISTS(SELECT 1 FROM memory_lifecycle_events) OR EXISTS(SELECT 1 FROM memory_items WHERE approval_origin<>'legacy') THEN RAISE EXCEPTION 'Preserve lifecycle records before downgrade'; END IF; END $$")
    for table in ('memory_lifecycle_events','memory_formation_proposals','memory_formation_jobs','memory_formation_settings'):
        op.execute(f'DROP TABLE {table}')
    op.execute('DROP FUNCTION aegis_memory_event_immutable()')
    op.execute('DROP FUNCTION aegis_memory_proposal_transition()')
    op.execute('ALTER TABLE memory_items DROP COLUMN approval_origin')
