"""逐次模型用途的记忆审计；不回填虚假的历史attempt或Token。"""
from alembic import op
from migrations.sql_runner import split_sql_statements

revision = '018_memory_call_usage'
down_revision = '017_memory_manual_lifecycle'
branch_labels = depends_on = None

DDL = """
ALTER TABLE run_execution_attempts
 ADD COLUMN memory_count integer,
 ADD COLUMN memory_policy_version varchar(64),
 ADD COLUMN memory_selection_reason varchar(64),
 ADD CONSTRAINT ck_attempt_memory_count CHECK (memory_count IS NULL OR memory_count>=0),
 ADD CONSTRAINT uq_attempt_memory_owner UNIQUE(id,run_id,tenant_id);
ALTER TABLE run_memory_usages
 ADD COLUMN attempt_id uuid,
 ADD COLUMN model_purpose varchar(32),
 ADD COLUMN memory_version integer,
 ADD COLUMN memory_type varchar(32),
 ADD COLUMN source_kind varchar(32),
 ADD COLUMN source_object_id uuid,
 ADD COLUMN source_version varchar(128),
 ADD COLUMN selection_reason varchar(64),
 ADD COLUMN policy_version varchar(64),
 ADD COLUMN excerpt_bytes integer,
 ADD COLUMN estimated_tokens bigint,
 ADD COLUMN call_prompt_tokens bigint,
 ADD COLUMN call_completion_tokens bigint,
 ADD COLUMN call_total_tokens bigint,
 ADD CONSTRAINT fk_memory_usage_attempt_owner FOREIGN KEY(attempt_id,run_id,tenant_id)
 REFERENCES run_execution_attempts(id,run_id,tenant_id),
 ADD CONSTRAINT ck_memory_usage_call CHECK(attempt_id IS NULL OR (
   model_purpose IS NOT NULL AND model_purpose IN ('conversation_reply','mail_reply_draft','mail_draft_revision')
   AND memory_version IS NOT NULL AND memory_version>0
   AND memory_type IS NOT NULL AND memory_type IN ('long_term_summary','entity')
   AND source_kind IS NOT NULL AND source_kind IN ('manual','run','knowledge_document')
   AND source_object_id IS NOT NULL AND source_version IS NOT NULL
   AND selection_reason IS NOT NULL AND policy_version IS NOT NULL
   AND excerpt_bytes IS NOT NULL AND excerpt_bytes>0 AND display_summary IS NULL)),
 ADD CONSTRAINT ck_memory_usage_tokens CHECK(
   (estimated_tokens IS NULL OR estimated_tokens>=0) AND
   (call_prompt_tokens IS NULL OR call_prompt_tokens>=0) AND
   (call_completion_tokens IS NULL OR call_completion_tokens>=0) AND
   (call_total_tokens IS NULL OR call_total_tokens>=0));
CREATE UNIQUE INDEX uq_memory_usage_attempt_item ON run_memory_usages(attempt_id,memory_id)
 WHERE attempt_id IS NOT NULL;
CREATE INDEX ix_memory_usage_run_purpose ON run_memory_usages(run_id,model_purpose,attempt_id);
"""


def upgrade():
    for statement in split_sql_statements(DDL):
        op.execute(statement)


def downgrade():
    # 先停新版Worker；降级会丢失新增关联，不改记忆正文或确认状态。
    op.execute('DROP INDEX ix_memory_usage_run_purpose')
    op.execute('DROP INDEX uq_memory_usage_attempt_item')
    for name in ('ck_memory_usage_tokens','ck_memory_usage_call','fk_memory_usage_attempt_owner'):
        op.execute(f'ALTER TABLE run_memory_usages DROP CONSTRAINT {name}')
    for name in ('attempt_id','model_purpose','memory_version','memory_type','source_kind',
                 'source_object_id','source_version','selection_reason','policy_version','excerpt_bytes',
                 'estimated_tokens','call_prompt_tokens','call_completion_tokens','call_total_tokens'):
        op.drop_column('run_memory_usages',name)
    for name in ('ck_attempt_memory_count','uq_attempt_memory_owner'):
        op.execute(f'ALTER TABLE run_execution_attempts DROP CONSTRAINT {name}')
    for name in ('memory_count','memory_policy_version','memory_selection_reason'):
        op.drop_column('run_execution_attempts',name)
