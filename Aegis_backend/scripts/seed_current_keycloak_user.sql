-- 当前 Keycloak Jason 用户的最小化本地身份映射数据。
-- 依赖：已执行 migrations/001_initial_schema.sql。
-- 用途：仅供本地开发验证受保护接口；不创建会话、邮件、日历或第三方连接数据。
-- 执行示例：psql -h 127.0.0.1 -U aegis_app -d aegis_pa -f scripts/seed_current_keycloak_user.sql

BEGIN;

-- 固定使用本地开发租户。按租户名称冲突时复用已有租户 ID。
WITH development_tenant AS (
  INSERT INTO tenants (id, name, status)
  VALUES ('10000000-0000-4000-8000-000000000001', 'aegis-dev', 'active')
  ON CONFLICT (name) DO UPDATE SET status = EXCLUDED.status
  RETURNING id
)
INSERT INTO users (
  id,
  tenant_id,
  oidc_issuer,
  oidc_subject,
  email,
  display_name,
  timezone,
  status
)
SELECT
  '10000000-0000-4000-8000-000000000010',
  id,
  'http://127.0.0.1:8080/realms/aegis',
  'c3c01f0d-8e70-4e25-b6f2-7eeec5bc9464',
  'jason@example.com',
  'Jason Li',
  'Asia/Shanghai',
  'active'
FROM development_tenant
ON CONFLICT (oidc_issuer, oidc_subject) DO UPDATE SET
  tenant_id = EXCLUDED.tenant_id,
  email = EXCLUDED.email,
  display_name = EXCLUDED.display_name,
  timezone = EXCLUDED.timezone,
  status = EXCLUDED.status;

-- 执行后应返回一条 active 用户记录，供后端按 issuer + sub 查找。
SELECT
  u.id AS user_id,
  u.tenant_id,
  t.name AS tenant_name,
  u.oidc_issuer,
  u.oidc_subject,
  u.email,
  u.status
FROM users AS u
JOIN tenants AS t ON t.id = u.tenant_id
WHERE u.oidc_issuer = 'http://127.0.0.1:8080/realms/aegis'
  AND u.oidc_subject = 'c3c01f0d-8e70-4e25-b6f2-7eeec5bc9464';

COMMIT;
