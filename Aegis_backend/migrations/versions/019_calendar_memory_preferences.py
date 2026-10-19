"""日历结构化软偏好和逐步骤用途；不自动新增或确认任何用户记忆。"""
from alembic import op

revision='019_calendar_memory_preferences'
down_revision='018_memory_call_usage'
branch_labels=depends_on=None

LIFECYCLE="""
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
          AND entity_key IN ({keys}) AND jsonb_typeof(entity_value)='object'
          AND entity_value->>'key' IS NOT NULL AND entity_value->>'key'=entity_key))
"""
USAGE="""
attempt_id IS NULL OR (
 model_purpose IS NOT NULL AND model_purpose IN ({purposes})
 AND memory_version IS NOT NULL AND memory_version>0
 AND memory_type IS NOT NULL AND memory_type IN ('long_term_summary','entity')
 AND source_kind IS NOT NULL AND source_kind IN ('manual','run','knowledge_document')
 AND source_object_id IS NOT NULL AND source_version IS NOT NULL
 AND selection_reason IS NOT NULL AND policy_version IS NOT NULL
 AND excerpt_bytes IS NOT NULL AND excerpt_bytes>0 AND display_summary IS NULL
 AND (model_purpose NOT IN ('calendar_planning','calendar_draft_personalization','calendar_recommendation')
      OR memory_type='entity'))
"""
KEYS="'timezone','contact','reply_style'"
PURPOSES="'conversation_reply','mail_reply_draft','mail_draft_revision'"


def change(keys,purposes):
    op.execute('ALTER TABLE memory_items DROP CONSTRAINT ck_memory_lifecycle')
    op.execute('ALTER TABLE memory_items ADD CONSTRAINT ck_memory_lifecycle CHECK ('+LIFECYCLE.format(keys=keys)+')')
    op.execute('ALTER TABLE run_memory_usages DROP CONSTRAINT ck_memory_usage_call')
    op.execute('ALTER TABLE run_memory_usages ADD CONSTRAINT ck_memory_usage_call CHECK ('+USAGE.format(purposes=purposes)+')')


def upgrade():
    change(KEYS+",'meeting_preferences'",PURPOSES+",'calendar_planning','calendar_draft_personalization','calendar_recommendation'")


def downgrade():
    # 包括已删除的实体/历史审计：不为降级偷偷删除用户数据。
    op.execute("""DO $$ BEGIN
      IF EXISTS(SELECT 1 FROM memory_items WHERE entity_key='meeting_preferences') OR
         EXISTS(SELECT 1 FROM run_memory_usages WHERE model_purpose IN
           ('calendar_planning','calendar_draft_personalization','calendar_recommendation')) THEN
        RAISE EXCEPTION '019 downgrade requires manual export and handling of calendar memory records';
      END IF;
    END $$""")
    change(KEYS,PURPOSES)
