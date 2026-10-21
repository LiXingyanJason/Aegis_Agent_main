"""补充问题结构化单选与版本绑定快照；旧问题保持文本输入。"""
from alembic import op

revision="021_structured_input_choices"
down_revision="020_memory_selection_revoke"
branch_labels=depends_on=None


def upgrade():
    op.execute("ALTER TABLE run_input_requests ADD COLUMN input_type varchar(20) NOT NULL DEFAULT 'text'")
    op.execute("ALTER TABLE run_input_requests ADD COLUMN options jsonb NOT NULL DEFAULT '[]'::jsonb")
    op.execute("ALTER TABLE run_input_requests ADD COLUMN selected_option_id varchar(128)")
    op.execute("""ALTER TABLE run_input_requests ADD CONSTRAINT ck_input_choice_shape CHECK (
      jsonb_typeof(options)='array' AND (
        (input_type='text' AND jsonb_array_length(options)=0 AND selected_option_id IS NULL)
        OR (input_type='single_choice' AND jsonb_array_length(options) BETWEEN 1 AND 20 AND response IS NULL)
      ))""")


def downgrade():
    op.execute("""DO $$ BEGIN IF EXISTS (
      SELECT 1 FROM run_input_requests q JOIN agent_runs ar ON ar.id=q.run_id AND ar.tenant_id=q.tenant_id
      WHERE q.input_type='single_choice' AND ar.status NOT IN ('completed','failed','cancelled')
    ) THEN RAISE EXCEPTION 'Drain active single-choice runs before downgrade'; END IF; END $$""")
    op.execute("ALTER TABLE run_input_requests DROP CONSTRAINT ck_input_choice_shape")
    op.execute("ALTER TABLE run_input_requests DROP COLUMN selected_option_id, DROP COLUMN options, DROP COLUMN input_type")
