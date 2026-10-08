-- 为本地 Keycloak Jason 测试用户插入开发期长期记忆示例。
-- 依赖：已执行初始化用户脚本和 Alembic 迁移 006_memory_source_run。
-- 本脚本可重复执行：相同用户、相同文本的未删除记忆不会重复插入。
-- 不应在生产环境写入这些示例数据。

BEGIN;

WITH target_user AS (
  SELECT id, tenant_id
  FROM users
  WHERE oidc_issuer = 'http://127.0.0.1:8080/realms/aegis'
    AND oidc_subject = 'c3c01f0d-8e70-4e25-b6f2-7eeec5bc9464'
), sample_memories(content, is_sensitive) AS (
  VALUES
    ('默认使用北京时间。', false),
    ('周一上午不安排会议。', false),
    ('给客户的邮件语气保持正式、简洁。', false),
    ('项目会议优先安排在工作日 14:00 至 17:00。', false),
    ('个人紧急联系方式属于敏感信息，仅在我明确要求时使用。', true)
)
INSERT INTO memory_items (tenant_id, user_id, content, source, is_sensitive)
SELECT target_user.tenant_id, target_user.id, sample_memories.content, 'user_input', sample_memories.is_sensitive
FROM target_user
CROSS JOIN sample_memories
WHERE NOT EXISTS (
  SELECT 1
  FROM memory_items existing
  WHERE existing.tenant_id = target_user.tenant_id
    AND existing.user_id = target_user.id
    AND existing.content = sample_memories.content
    AND existing.deleted_at IS NULL
);

COMMIT;
