"""Personal Skill drafts, immutable releases and per-call references."""
from alembic import op
from migrations.sql_runner import split_sql_statements

revision = '022_skills'
down_revision = '021_structured_input_choices'
branch_labels = depends_on = None

DDL = """
CREATE TABLE skills (
 id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants(id),
 user_id uuid NOT NULL REFERENCES users(id), name varchar(64) NOT NULL,
 description varchar(300) NOT NULL, body text NOT NULL, purposes jsonb NOT NULL,
 revision integer NOT NULL DEFAULT 1 CHECK(revision>0), published_version integer,
 enabled boolean NOT NULL DEFAULT false, deleted_at timestamptz,
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(id,tenant_id,user_id), CHECK(length(body) BETWEEN 1 AND 6000),
 CHECK(NOT enabled OR published_version IS NOT NULL)
);
CREATE UNIQUE INDEX uq_skill_owner_name ON skills(tenant_id,user_id,name) WHERE deleted_at IS NULL;
CREATE TABLE skill_versions (
 skill_id uuid NOT NULL, tenant_id uuid NOT NULL, user_id uuid NOT NULL,
 version integer NOT NULL CHECK(version>0), name varchar(64) NOT NULL,
 description varchar(300) NOT NULL, body text NOT NULL, purposes jsonb NOT NULL,
 content_hash varchar(64) NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(skill_id,version),
 FOREIGN KEY(skill_id,tenant_id,user_id) REFERENCES skills(id,tenant_id,user_id)
);
CREATE TABLE skill_events (
 id uuid PRIMARY KEY, skill_id uuid NOT NULL, tenant_id uuid NOT NULL, user_id uuid NOT NULL,
 revision integer NOT NULL, action varchar(24) NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
 FOREIGN KEY(skill_id,tenant_id,user_id) REFERENCES skills(id,tenant_id,user_id)
);
ALTER TABLE skills ADD CONSTRAINT fk_skill_published_version
 FOREIGN KEY(id,published_version) REFERENCES skill_versions(skill_id,version);
CREATE FUNCTION aegis_immutable_skill_version() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'Skill releases are immutable'; END $$;
CREATE TRIGGER immutable_skill_version BEFORE UPDATE OR DELETE ON skill_versions
 FOR EACH ROW EXECUTE FUNCTION aegis_immutable_skill_version();
CREATE TABLE run_skill_bindings (
 run_id uuid NOT NULL REFERENCES agent_runs(id), tenant_id uuid NOT NULL, user_id uuid NOT NULL,
 purpose varchar(32) NOT NULL CHECK(purpose IN ('mail_reply_draft','mail_draft_revision')),
 status varchar(24) NOT NULL CHECK(status IN ('selecting','selected','none')),
 skill_id uuid, version integer, content_hash varchar(64),
 reads integer NOT NULL DEFAULT 0 CHECK(reads BETWEEN 0 AND 1), reason varchar(64) NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(run_id,purpose),
 FOREIGN KEY(skill_id,version) REFERENCES skill_versions(skill_id,version),
 CHECK(status<>'selected' OR (skill_id IS NOT NULL AND version IS NOT NULL AND content_hash IS NOT NULL))
);
CREATE TABLE run_skill_usages (
 attempt_id uuid PRIMARY KEY, run_id uuid NOT NULL, tenant_id uuid NOT NULL, user_id uuid NOT NULL,
 purpose varchar(32) NOT NULL, skill_id uuid NOT NULL, version integer NOT NULL,
 content_hash varchar(64) NOT NULL, loaded_chars integer NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(),
 FOREIGN KEY(attempt_id,run_id,tenant_id) REFERENCES run_execution_attempts(id,run_id,tenant_id),
 FOREIGN KEY(skill_id,version) REFERENCES skill_versions(skill_id,version)
);
"""

def upgrade():
    for sql in split_sql_statements(DDL):
        op.execute(sql)
    for table in ('skills','skill_versions','skill_events','run_skill_bindings','run_skill_usages'):
        op.execute(f'ALTER TABLE {table} ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE {table} FORCE ROW LEVEL SECURITY')
        op.execute(f'''CREATE POLICY {table}_owner ON {table}
            USING(tenant_id=aegis_current_tenant_id() AND user_id=aegis_current_user_id())
            WITH CHECK(tenant_id=aegis_current_tenant_id() AND user_id=aegis_current_user_id())''')

def downgrade():
    op.execute("""DO $$ BEGIN IF EXISTS(SELECT 1 FROM skills) THEN
        RAISE EXCEPTION 'Export Skill data before downgrade'; END IF; END $$""")
    op.execute('ALTER TABLE skills DROP CONSTRAINT fk_skill_published_version')
    for table in ('run_skill_usages','run_skill_bindings','skill_events','skill_versions','skills'):
        op.drop_table(table)
    op.execute('DROP FUNCTION aegis_immutable_skill_version()')
