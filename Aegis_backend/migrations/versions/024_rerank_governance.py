"""Dedicated rerank count/attempts and separate document external permission."""
from alembic import op

revision='024_rerank_governance'
down_revision='023_shadow_index_generations'
branch_labels=depends_on=None


def upgrade():
    op.execute('ALTER TABLE run_execution_budgets ADD COLUMN rerank_calls integer NOT NULL DEFAULT 0 CHECK(rerank_calls>=0)')
    op.execute('ALTER TABLE run_execution_attempts DROP CONSTRAINT run_execution_attempts_kind_check')
    op.execute("ALTER TABLE run_execution_attempts ADD CONSTRAINT run_execution_attempts_kind_check CHECK(kind IN ('model','tool','rerank'))")
    op.execute('ALTER TABLE run_execution_attempts ADD COLUMN rerank_metadata jsonb')
    op.execute('ALTER TABLE knowledge_documents ADD COLUMN external_rerank_allowed boolean NOT NULL DEFAULT false')


def downgrade():
    op.execute("""DO $$ BEGIN IF EXISTS(SELECT 1 FROM run_execution_attempts WHERE kind='rerank')
      OR EXISTS(SELECT 1 FROM run_execution_budgets WHERE rerank_calls>0)
      OR EXISTS(SELECT 1 FROM knowledge_documents WHERE external_rerank_allowed) THEN
      RAISE EXCEPTION 'Preserve rerank audit/permissions; downgrade application first'; END IF; END $$""")
    op.execute('ALTER TABLE knowledge_documents DROP COLUMN external_rerank_allowed')
    op.execute('ALTER TABLE run_execution_attempts DROP COLUMN rerank_metadata')
    op.execute('ALTER TABLE run_execution_attempts DROP CONSTRAINT run_execution_attempts_kind_check')
    op.execute("ALTER TABLE run_execution_attempts ADD CONSTRAINT run_execution_attempts_kind_check CHECK(kind IN ('model','tool'))")
    op.execute('ALTER TABLE run_execution_budgets DROP COLUMN rerank_calls')
