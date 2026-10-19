"""排名安全诊断及旧审计正文清理；无时间衰减、不修改记忆正文。"""
from alembic import op
from migrations.sql_runner import split_sql_statements

revision='020_memory_selection_revoke'
down_revision='019_calendar_memory_preferences'
branch_labels=depends_on=None

DDL="""
ALTER TABLE run_execution_attempts ADD COLUMN memory_selection_details jsonb,
 ADD CONSTRAINT ck_memory_selection_details CHECK(memory_selection_details IS NULL OR
   (jsonb_typeof(memory_selection_details)='object' AND octet_length(memory_selection_details::text)<=24000));
CREATE INDEX ix_memory_recall_owner ON memory_items(tenant_id,user_id,memory_type,entity_key)
 WHERE deleted_at IS NULL AND status='confirmed' AND NOT is_sensitive AND sensitivity='normal';
UPDATE run_memory_usages SET display_summary=NULL WHERE display_summary IS NOT NULL;
ALTER TABLE run_memory_usages ADD CONSTRAINT ck_memory_usage_no_text CHECK(display_summary IS NULL);
CREATE INDEX ix_memory_usage_scrub ON run_memory_usages(tenant_id,memory_id) WHERE display_summary IS NOT NULL;
CREATE FUNCTION aegis_scrub_memory_usage_text() RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER AS $$
BEGIN
 UPDATE run_memory_usages SET display_summary=NULL
 WHERE tenant_id=NEW.tenant_id AND memory_id=NEW.id AND display_summary IS NOT NULL;
 RETURN NEW;
END $$;
CREATE TRIGGER memory_usage_text_scrub AFTER UPDATE OF content,status,is_sensitive,sensitivity,
 expires_at,source_kind,source_object_id,source_version,deleted_at ON memory_items
 FOR EACH ROW EXECUTE FUNCTION aegis_scrub_memory_usage_text();
"""


def upgrade():
    for statement in split_sql_statements(DDL): op.execute(statement)


def downgrade():
    op.execute('DROP TRIGGER memory_usage_text_scrub ON memory_items')
    op.execute('DROP FUNCTION aegis_scrub_memory_usage_text()')
    op.execute('DROP INDEX ix_memory_recall_owner')
    op.execute('DROP INDEX ix_memory_usage_scrub')
    op.execute('ALTER TABLE run_memory_usages DROP CONSTRAINT ck_memory_usage_no_text')
    op.execute('ALTER TABLE run_execution_attempts DROP CONSTRAINT ck_memory_selection_details')
    op.drop_column('run_execution_attempts','memory_selection_details')
    # 不恢复清理过的旧审计正文；降级前须停止新版Worker并备份。
