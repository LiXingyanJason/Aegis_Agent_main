-- 为本地 Keycloak Jason 测试用户创建开发期 Mock Calendar 连接。
-- 依赖：已执行 001_initial_schema.sql 和 seed_current_keycloak_user.sql。
-- 不保存真实 OAuth 凭据；credential_ref 仅是 Mock 服务标识。

BEGIN;

INSERT INTO provider_connections (
  id, tenant_id, user_id, provider, provider_account_id,
  credential_ref, scopes, status, last_verified_at
)
SELECT
  '10000000-0000-4000-8000-000000000020',
  u.tenant_id,
  u.id,
  'google_calendar',
  'mock-jason-calendar',
  'mock://calendar/jason',
  '["calendar.read", "calendar.write"]'::jsonb,
  'active',
  now()
FROM users AS u
WHERE u.oidc_issuer = 'http://127.0.0.1:8080/realms/aegis'
  AND u.oidc_subject = 'c3c01f0d-8e70-4e25-b6f2-7eeec5bc9464'
ON CONFLICT (tenant_id, user_id, provider, provider_account_id) DO UPDATE SET
  credential_ref = EXCLUDED.credential_ref,
  scopes = EXCLUDED.scopes,
  status = 'active',
  last_verified_at = now();

COMMIT;
