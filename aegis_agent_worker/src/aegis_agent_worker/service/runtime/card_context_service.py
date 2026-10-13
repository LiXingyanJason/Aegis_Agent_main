"""卡片安全读取和最终目标复核；不做自然语言解析，也不调用LLM。"""
import json
from uuid import UUID
from pydantic import ValidationError

from aegis_agent_worker.agent.contracts.context import CardsGetArgs, CardsListArgs, CardSnapshot, CardListResult
from aegis_agent_worker.config.database import TenantContext, tenant_transaction
from aegis_agent_worker.repository.conversation_card_repository import ConversationCardRepository
from aegis_agent_worker.repository.conversation_artifact_repository import normalize_artifact_payload
from aegis_agent_worker.tool.contracts import ToolError, ToolContext
from aegis_agent_worker.harness.policy_provider import DEFAULT_POLICIES


class CardContextService:
    def __init__(self, database, repository=None, policy_provider=None):
        self._database = database
        self._repository = repository or ConversationCardRepository()
        self._policies = policy_provider or DEFAULT_POLICIES

    async def read(self, context, tool_name, arguments):
        args = (CardsListArgs if tool_name == "conversation.cards.list" else CardsGetArgs).model_validate(arguments)
        async with self._database.session() as session:
            async with tenant_transaction(session, TenantContext(context.tenant_id, context.user_id)):
                conversation = await self._repository.conversation_for_run(session, context)
                if conversation is None:
                    raise ToolError("CARD_NOT_FOUND", "卡片不存在或无权访问")
                if tool_name == "conversation.cards.list":
                    rows = await self._repository.list_cards(session, context, conversation, args.card_type, args.limit)
                    return self._safe_output(CardListResult, {"cards": [
                        {**self._metadata(row), "item_count": row["item_count"]} for row in rows
                    ]})
                row = await self._repository.get_card(session, context, conversation, args)
                if row is None:
                    raise ToolError("CARD_NOT_FOUND", "卡片不存在或无权访问")
                output = self._metadata(row)
                output["items"] = []
                if not row["expired"]:
                    payload = normalize_artifact_payload(row["payload"])
                    # 关联必须与真实run一致，不信任payload里的任意query_id。
                    if str(payload.get("query_id")) != str(row["run_id"]):
                        raise ToolError("CARD_INVALID", "卡片引用格式无效，请重新查询")
                    items = payload.get("items", [])
                    if not isinstance(items, list) or len(items) > 20:
                        raise ToolError("CARD_INVALID", "卡片条目格式无效")
                    positions = set()
                    for item in items:
                        if not isinstance(item, dict):
                            raise ToolError("CARD_INVALID", "卡片条目格式无效")
                        position = item.get("position")
                        if type(position) is not int or not 1 <= position <= 20 or position in positions:
                            raise ToolError("CARD_INVALID", "卡片位置无效")
                        positions.add(position)
                        try:
                            email_id = UUID(str(item["email_id"]))
                        except (KeyError, ValueError):
                            raise ToolError("CARD_INVALID", "卡片引用格式无效") from None
                        current = await self._repository.current_email(session, context, email_id)
                        version = item.get("source_version")
                        valid = current is not None and isinstance(version, str) and 0 < len(version) <= 128
                        valid = valid and current["source_version"] == version
                        # 失效位置保留，但不再提供可执行ID／版本，也不重排后续位置。
                        safe = {"position": position, "status": "valid" if valid else "stale"}
                        if valid:
                            safe.update(email_id=str(email_id), source_version=version,
                                        subject=str(item.get("subject") or "")[:200])
                        output["items"].append(safe)
                if len(json.dumps(output, ensure_ascii=False).encode()) > self._policies.current().output.card_result_bytes:
                    raise ToolError("CARD_CONTEXT_TOO_LARGE", "卡片上下文过大")
                return self._safe_output(CardSnapshot, output)

    def _safe_output(self, schema, output):
        try:
            payload = schema.model_validate(output).model_dump(mode="json", exclude_none=True)
            if len(json.dumps(payload, ensure_ascii=False).encode()) > self._policies.current().output.card_result_bytes:
                raise ToolError("CARD_CONTEXT_TOO_LARGE", "卡片上下文过大")
            return payload
        except ValidationError:
            raise ToolError("CARD_INVALID", "卡片安全输出未通过校验，请重新查询") from None

    @staticmethod
    def _metadata(row):
        return {"card_id": str(row["id"]), "query_id": str(row["run_id"]),
                "card_type": row["artifact_type"], "status": "expired" if row["expired"] else "valid",
                "created_at": row["created_at"].isoformat(), "expires_at": row["expires_at"].isoformat()}

    async def resolve(self, run, reference, read_results):
        # 只允许引用本次成功get读取的卡片；list索引不是执行引用。
        offered = next((item for item in read_results if item.get("card_id") == str(reference.card_id)
                        and item.get("query_id") == str(reference.query_id) and item.get("status") == "valid"), None)
        if offered is None:
            return None
        prior = next((item for item in offered.get("items", []) if item.get("position") == reference.position
                      and item.get("status") == "valid"), None)
        if prior is None:
            return None
        context = ToolContext(run.tenant_id, run.user_id, run.id)
        try:
            current = await self.read(context, "conversation.cards.get", {"card_id": str(reference.card_id)})
        except ToolError:
            return None
        item = next((item for item in current.get("items", []) if item.get("position") == reference.position), None)
        if (current["query_id"] != str(reference.query_id) or current["status"] != "valid"
                or item is None or item.get("status") != "valid"
                or item.get("email_id") != prior.get("email_id")
                or item.get("source_version") != prior.get("source_version")):
            return None
        return {"resource_type": "email", "id": item["email_id"], "source_version": item["source_version"]}
