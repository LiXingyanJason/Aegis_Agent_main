"""邮件列表接口测试。"""

from datetime import UTC, datetime
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.controller.mail_controller import get_mail_service_from_request, router
from app.repository.user_repository import LocalUser
from app.security.dependencies import CurrentUser, get_current_user, get_tenant_session
from app.service.mail_service import (
    MailConnectionRequiredError,
    MailAsyncRequest,
    MailListSnapshot,
    MailToolUnavailableError,
)
from app.service.run_dispatch_service import get_run_dispatcher_from_request


LOCAL_USER = LocalUser(
    id=UUID("b0071771-d192-4c32-a4e0-7b214eec2be6"),
    tenant_id=UUID("2f744d3e-2a54-4d4f-aec3-bc9f0e2b6966"),
    email="jason@example.com",
    display_name="Jason",
)
EMAIL_ID = UUID("a8d1baa4-1fc3-4cc9-8500-b43aebff034e")
DRAFT_ID = UUID("39e6a50e-933a-423b-bf66-c9825587d389")
RUN_ID = UUID("5b67672a-3042-44cf-a567-1720216a4d1a")
EXTRACTION_ID = UUID("0e5f9408-d763-4b60-a77f-48849f4ee3be")


class _FakeMailService:
    """模拟邮件同步服务，避免 Controller 测试访问真实数据库和 MCP 服务。"""

    def __init__(self, result: MailListSnapshot | Exception) -> None:
        self._result = result
        self.received_user: LocalUser | None = None

    async def sync_and_list_messages(self, _session, local_user: LocalUser) -> MailListSnapshot:
        """返回预设同步结果，并记录 Controller 传入的认证用户。"""
        self.received_user = local_user
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class _FakeMailManagementService:
    """模拟草稿、已发送和待办草稿的站内管理能力。"""

    async def list_drafts(self, _session, _local_user):
        """返回一条列表草稿；列表中故意没有正文。"""
        return [
            {
                "id": DRAFT_ID,
                "source_email_id": EMAIL_ID,
                "subject": "续约确认回复",
                "version": 2,
                "status": "draft",
                "updated_at": datetime(2026, 10, 8, 3, 0, tzinfo=UTC),
                "recipients": {"to": ["lili@northstar.example.com"], "cc": [], "bcc": []},
            }
        ]

    async def get_draft(self, _session, _local_user, draft_id):
        """只对预设草稿返回详情。"""
        if draft_id != DRAFT_ID:
            return None
        item = (await self.list_drafts(_session, _local_user))[0]
        return {**item, "body": "您好，续约方案正在确认中。"}

    async def update_draft(self, _session, _local_user, draft_id, param):
        """模拟保存成功后的版本递增。"""
        if draft_id != DRAFT_ID:
            return None
        return {
            "id": DRAFT_ID,
            "source_email_id": EMAIL_ID,
            "subject": param.subject,
            "body": param.body,
            "version": 3,
            "status": "draft",
            "updated_at": datetime(2026, 10, 8, 4, 0, tzinfo=UTC),
            "recipients": {"to": param.to, "cc": param.cc, "bcc": []},
        }

    async def list_sent_messages(self, _session, _local_user):
        """模拟空的已发送分区。"""
        return []

    async def create_todo_drafts(self, _session, _local_user, email_id, param):
        """模拟将第一项候选复制为站内待办草稿。"""
        if email_id != EMAIL_ID or param.todo_indexes != [0]:
            return None
        return [
            {
                "id": UUID("7c0bd722-513f-4324-9e25-e2b095bd81e7"),
                "email_id": EMAIL_ID,
                "extraction_id": param.extraction_id,
                "content": "确认续约报价",
                "due_at": None,
                "status": "draft",
                "created_at": datetime(2026, 10, 8, 4, 0, tzinfo=UTC),
            }
        ]

    async def request_extraction(self, _session, _local_user, email_id, param):
        """模拟创建摘要任务，并保留 refresh 参数可供断言。"""
        if email_id != EMAIL_ID:
            return None
        return MailAsyncRequest(
            status="queued", run_id=RUN_ID, extraction_id=EXTRACTION_ID, cached=False
        )

    async def request_reply_draft(self, _session, _local_user, email_id, param):
        """模拟创建回复草稿任务。"""
        if email_id != EMAIL_ID:
            return None
        return MailAsyncRequest(status="queued", run_id=RUN_ID)


class _FakeDispatcher:
    """记录 Controller 在事务提交后应该投递的 Agent 任务。"""

    def __init__(self) -> None:
        self.enqueued: list[UUID] = []

    def enqueue_after_commit(self, run_id: UUID, **_kwargs) -> None:
        """测试中只记录投递意图，不访问 Redis。"""
        self.enqueued.append(run_id)


def _create_test_app(fake_mail_service) -> FastAPI:
    """创建已替换认证、数据库和邮件服务依赖的测试应用。"""
    app = FastAPI()
    app.include_router(router)

    async def override_current_user() -> CurrentUser:
        """模拟完成 OIDC 与本地用户映射的身份。"""
        return CurrentUser(user=LOCAL_USER)

    async def override_tenant_session():
        """模拟已经设置 RLS 上下文的数据库会话。"""
        yield object()

    def override_mail_service() -> _FakeMailService:
        """注入不发起真实 MCP 请求的邮件服务。"""
        return fake_mail_service

    dispatcher = _FakeDispatcher()

    def override_dispatcher() -> _FakeDispatcher:
        """注入不依赖真实事务与 Redis 的任务投递器。"""
        return dispatcher

    app.dependency_overrides[get_current_user] = override_current_user
    app.dependency_overrides[get_tenant_session] = override_tenant_session
    app.dependency_overrides[get_mail_service_from_request] = override_mail_service
    app.dependency_overrides[get_run_dispatcher_from_request] = override_dispatcher
    app.state.fake_dispatcher = dispatcher
    return app


def test_list_messages_returns_current_users_snapshot_without_body() -> None:
    """测试邮件列表只返回已认证用户的元数据快照，不包含邮件正文。"""
    snapshot_at = datetime(2026, 10, 8, 2, 0, tzinfo=UTC)
    fake_service = _FakeMailService(
        MailListSnapshot(
            snapshot_at=snapshot_at,
            items=[
                {
                    "id": EMAIL_ID,
                    "provider_thread_id": "mock-thread-renewal-001",
                    "sender_name": "李莉",
                    "sender_email": "lili@northstar.example.com",
                    "subject": "2026 年服务续约确认",
                    "received_at": datetime(2026, 10, 6, 2, 10, tzinfo=UTC),
                    "source_version": "mock-v1",
                    "extraction_status": "not_generated",
                    "body_text": "这不应返回给浏览器",
                }
            ],
        )
    )
    app = _create_test_app(fake_service)

    with TestClient(app) as client:
        response = client.get("/mail/messages", headers={"Authorization": "Bearer verified-test-token"})

    assert response.status_code == 200
    item = response.json()["data"]["items"][0]
    assert item["email_id"] == str(EMAIL_ID)
    assert item["customer"] == "李莉"
    assert "body_text" not in item
    assert fake_service.received_user == LOCAL_USER


def test_list_messages_explains_missing_connection() -> None:
    """测试没有有效邮件读取连接时返回可供页面展示的连接提示。"""
    app = _create_test_app(_FakeMailService(MailConnectionRequiredError("尚未连接工作邮箱")))

    with TestClient(app) as client:
        response = client.get("/mail/messages", headers={"Authorization": "Bearer verified-test-token"})

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "CONNECTION_REQUIRED"
    assert response.json()["detail"]["required_scopes"] == ["mail.read"]


def test_list_messages_returns_503_when_email_tool_is_unavailable() -> None:
    """测试 MCP 服务不可用时不回退为虚构邮件，而是明确返回 503。"""
    app = _create_test_app(_FakeMailService(MailToolUnavailableError("邮件工具服务暂时不可用")))

    with TestClient(app) as client:
        response = client.get("/mail/messages", headers={"Authorization": "Bearer verified-test-token"})

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "MAIL_TOOL_UNAVAILABLE"


def test_mail_draft_list_and_update_expose_only_expected_content() -> None:
    """测试草稿列表不返回正文，详情和保存接口返回站内草稿的最新版本。"""
    app = _create_test_app(_FakeMailManagementService())

    with TestClient(app) as client:
        listed = client.get("/mail/drafts", headers={"Authorization": "Bearer verified-test-token"})
        detail = client.get(f"/mail/drafts/{DRAFT_ID}", headers={"Authorization": "Bearer verified-test-token"})
        updated = client.patch(
            f"/mail/drafts/{DRAFT_ID}",
            headers={"Authorization": "Bearer verified-test-token"},
            json={
                "to": ["lili@northstar.example.com"],
                "cc": [],
                "subject": "续约确认回复（已更新）",
                "body": "您好，预计明日确认。",
            },
        )

    assert listed.status_code == 200
    assert "body" not in listed.json()["data"][0]
    assert detail.status_code == 200
    assert detail.json()["data"]["body"] == "您好，续约方案正在确认中。"
    assert updated.status_code == 200
    assert updated.json()["data"]["version"] == 3


def test_sent_mail_list_and_todo_draft_routes_are_available() -> None:
    """测试已发送空状态与不触发 LLM 的待办草稿复制接口。"""
    app = _create_test_app(_FakeMailManagementService())
    extraction_id = EXTRACTION_ID

    with TestClient(app) as client:
        sent = client.get("/mail/sent-messages", headers={"Authorization": "Bearer verified-test-token"})
        todo = client.post(
            f"/mail/{EMAIL_ID}/todo-drafts",
            headers={"Authorization": "Bearer verified-test-token"},
            json={"extraction_id": str(extraction_id), "todo_indexes": [0]},
        )

    assert sent.status_code == 200
    assert sent.json() == {"data": []}
    assert todo.status_code == 201
    assert todo.json()["data"][0]["extraction_id"] == str(extraction_id)


def test_extraction_and_reply_draft_requests_return_run_sse_urls() -> None:
    """测试两个 LLM 后台请求均返回可订阅的 run，并登记入队。"""
    app = _create_test_app(_FakeMailManagementService())

    with TestClient(app) as client:
        extraction = client.post(
            f"/mail/{EMAIL_ID}/extraction-requests",
            headers={"Authorization": "Bearer verified-test-token"},
            json={"refresh": False},
        )
        reply = client.post(
            f"/mail/{EMAIL_ID}/reply-draft-requests",
            headers={"Authorization": "Bearer verified-test-token"},
            json={"extraction_id": str(EXTRACTION_ID), "instruction": "语气简洁"},
        )

    assert extraction.status_code == 202
    assert extraction.json()["data"] == {
        "status": "queued",
        "run_id": str(RUN_ID),
        "extraction_id": str(EXTRACTION_ID),
        "cached": False,
        "events_url": f"/runs/{RUN_ID}/events",
        "extraction": None,
    }
    assert reply.status_code == 202
    assert reply.json()["data"]["run_id"] == str(RUN_ID)
    assert app.state.fake_dispatcher.enqueued == [RUN_ID, RUN_ID]
