"""Embedding 专属预算与逐次审计，支持真实 run 或索引 job，不伪装聊天任务。"""
from alembic import op
from migrations.sql_runner import split_sql_statements

revision = "011_embedding_governance"
down_revision = "010_knowledge_schema"
branch_labels = depends_on = None

DDL = """
CREATE TABLE embedding_execution_budgets (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES tenants(id), user_id uuid NOT NULL REFERENCES users(id),
 run_id uuid UNIQUE REFERENCES agent_runs(id), index_job_id uuid UNIQUE REFERENCES knowledge_index_jobs(id),
 policy_version varchar(64) NOT NULL, model_provider varchar(64) NOT NULL, model_name varchar(128) NOT NULL,
 model_version varchar(64) NOT NULL, dimensions integer NOT NULL CHECK(dimensions BETWEEN 1 AND 2000),
 call_count integer NOT NULL DEFAULT 0 CHECK(call_count>=0), input_chars bigint NOT NULL DEFAULT 0 CHECK(input_chars>=0),
 CHECK((run_id IS NULL) <> (index_job_id IS NULL)), UNIQUE(id,tenant_id,user_id)
);
CREATE TABLE embedding_execution_attempts (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), budget_id uuid NOT NULL, tenant_id uuid NOT NULL, user_id uuid NOT NULL,
 purpose varchar(32) NOT NULL CHECK(purpose IN ('knowledge_index','knowledge_query')),
 status varchar(16) NOT NULL DEFAULT 'running' CHECK(status IN ('running','succeeded','failed')),
 input_chars bigint NOT NULL CHECK(input_chars>0), batch_items integer NOT NULL CHECK(batch_items>0),
 prompt_tokens bigint CHECK(prompt_tokens>=0), total_tokens bigint CHECK(total_tokens>=0),
 duration_ms bigint CHECK(duration_ms>=0), error_code varchar(64),
 started_at timestamptz NOT NULL DEFAULT now(), finished_at timestamptz,
 FOREIGN KEY(budget_id,tenant_id,user_id) REFERENCES embedding_execution_budgets(id,tenant_id,user_id) ON DELETE CASCADE
);
CREATE INDEX ix_embedding_attempts_budget ON embedding_execution_attempts(budget_id,started_at);
"""


def upgrade():
    for statement in split_sql_statements(DDL):
        op.execute(statement)
    for table in ("embedding_execution_budgets", "embedding_execution_attempts"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        owner = "tenant_id=aegis_current_tenant_id() AND user_id=aegis_current_user_id()"
        op.execute(f"CREATE POLICY {table}_owner ON {table} USING ({owner}) WITH CHECK ({owner})")


def downgrade():
    op.execute("DROP TABLE embedding_execution_attempts")
    op.execute("DROP TABLE embedding_execution_budgets")
