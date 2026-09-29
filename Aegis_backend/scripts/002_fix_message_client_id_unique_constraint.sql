-- 修复 conversation_messages 的 client_message_id 唯一约束。
-- 适用对象：已经执行过 001_initial_schema.sql 的现有数据库。
-- 原约束使用 UNIQUE NULLS NOT DISTINCT，错误地限制了一场会话只能有一条 NULL client_message_id 消息。
-- 用户消息以 client_message_id 进行幂等去重；assistant/system/tool 消息没有该标识，必须允许多条。

BEGIN;

ALTER TABLE conversation_messages
  DROP CONSTRAINT IF EXISTS conversation_messages_conversation_id_client_message_id_key;

CREATE UNIQUE INDEX IF NOT EXISTS ux_conversation_messages_client_message_id_present
  ON conversation_messages (conversation_id, client_message_id)
  WHERE client_message_id IS NOT NULL;

COMMIT;
