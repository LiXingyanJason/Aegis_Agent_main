"""固定主路由评测。默认离线；旧新版在独立进程用同一评测集，绝不执行业务工具。"""
import argparse
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
import hashlib
import inspect
import json
from pathlib import Path
import subprocess
import sys
from time import monotonic
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID, uuid4

from routing_eval.metrics import summarize

ROOT = Path(__file__).resolve().parents[1]
EMAIL = "00000000-0000-0000-0000-000000000009"
CARD = "00000000-0000-0000-0000-000000000090"
QUERY = "00000000-0000-0000-0000-000000000091"
CAPS = [("calendar", "list_events", "calendar_read"),
        ("calendar", "find_free_time", "calendar_read"),
        ("calendar", "arrange_meeting", "calendar_confirmation"),
        ("mail", "query_mail", "mail_query")]


def load_cases(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    ids = [case["id"] for case in data["cases"]]
    if len(set(ids)) != len(ids):
        raise ValueError("评测用例 ID 重复")
    for case in data["cases"]:
        if not case.get("allowed") or case["scenario"] not in {"semantic", "fault", "page"}:
            raise ValueError("评测用例缺少允许结果或场景")
    return data


def verify_source_revision(source, revision):
    """历史源码通过 Git blob 校验，避免导出目录误标成父仓库 HEAD。"""
    command = ["git", "-C", str(ROOT.parent)]
    commit = subprocess.check_output([*command, "rev-parse", revision+"^{commit}"], text=True).strip()
    tree = subprocess.check_output([*command, "ls-tree", "-r", commit, "aegis_agent_worker/src"], text=True)
    expected = {}
    for line in tree.splitlines():
        metadata, path = line.split("\t", 1)
        if path.endswith(".py"):
            expected[path.removeprefix("aegis_agent_worker/")] = metadata.split()[2]
    actual = {}
    for path in (source/"src").rglob("*.py"):
        content = path.read_bytes()
        relative = path.relative_to(source).as_posix()
        blob = lambda value: hashlib.sha1(b"blob "+str(len(value)).encode()+b"\0"+value).hexdigest()
        raw = blob(content)
        # Windows 工作副本可能由 autocrlf 转成 CRLF；只允许这一种行尾等价转换。
        actual[relative] = raw if raw == expected.get(relative) else blob(content.replace(b"\r\n", b"\n"))
    if not expected or actual != expected:
        raise ValueError("被测源码不匹配指定 Git revision，拒绝标记历史版本")
    return commit


class MemoryDatabase:
    @asynccontextmanager
    async def session(self):
        class Session:
            @asynccontextmanager
            async def begin(self):
                yield
            async def execute(self, sql, *args):
                if "set_config" not in str(sql):
                    raise RuntimeError("评测禁止真实 SQL")
        yield Session()


class MemoryBudget:
    """只替换 Repository；生产 Harness 仍负责预算／超时及调用上下文。"""
    def __init__(self):
        self.used = {"model": 0, "tool": 0}
        self.audits = []
    async def ensure_budget(self, session, run, version):
        return version
    async def add_active_time(self, *args):
        pass
    async def reserve_call(self, session, run, **kwargs):
        kind = kwargs["kind"]
        if self.used[kind] >= kwargs["maximum"]:
            return None
        self.used[kind] += 1
        return 0
    async def start_attempt(self, session, run, **kwargs):
        return uuid4()
    async def finish_attempt(self, session, attempt_id, **kwargs):
        self.audits.append(kwargs)


class Facts:
    """同一快照供旧版预取和新版按需读取；不访问邮箱、数据库或 MCP。"""
    catalog_version = "eval-catalog-v1"
    def __init__(self, case):
        self.case, self.audits, self.reads, self.loaded = case, [], [], False
    def reference(self):
        return dict(resource_type="email", id=EMAIL, source_version="v1")
    async def load_references(self, run):
        return ([{**self.reference(), "label": "2026 年服务续约确认", "position": 1}]
                if self.case.get("card") == "valid" else [])
    async def validate_reference(self, run, reference):
        return self.reference() if (self.case.get("card") == "valid" and
                                   str(reference.id) == EMAIL and reference.source_version == "v1") else None
    async def record_decision(self, run, **kwargs):
        self.audits.append(kwargs)
    async def execute(self, run, request):
        async with self.harness.attempt("tool", request.tool_name):
            return await self._execute(run, request)

    async def _execute(self, run, request):
        from aegis_agent_worker.tool.contracts import ToolError
        if request.tool_name not in {"conversation.cards.list", "conversation.cards.get"}:
            raise ToolError("TOOL_SCOPE_BLOCKED", "评测禁止业务工具")
        self.reads.append(request.tool_name)
        if self.case.get("card") in {None, "missing", "foreign"}:
            raise ToolError("CARD_UNAVAILABLE", "卡片不可用")
        if request.arguments.get("card_id") not in {None, CARD}:
            raise ToolError("CARD_UNAVAILABLE", "卡片不可用")
        status = "expired" if self.case["card"] == "expired" else "valid"
        header = dict(card_id=CARD, query_id=QUERY, card_type="mail_candidates", status=status,
                      created_at="2026-10-14T00:00:00Z", expires_at="2026-10-15T00:00:00Z")
        if request.tool_name.endswith("list"):
            return {"cards": [{**header, "item_count": 1}]}
        self.loaded = True
        items = [] if status == "expired" else ([dict(position=1, status="stale")]
            if self.case["card"] == "stale" else [dict(position=1, status="valid", email_id=EMAIL,
                                                       source_version="v1", subject="2026 年服务续约确认")])
        return {**header, "items": items}
    async def resolve(self, run, reference, results):
        return self.reference() if (self.loaded and self.case.get("card") == "valid" and
            str(reference.card_id) == CARD and str(reference.query_id) == QUERY and reference.position == 1) else None


class RecordingModel:
    """包裹同一版本的生产通信客户端，共同采集 HTTP usage，兼容没有 usage 属性的旧版。"""
    def __init__(self, case, settings, live, quota):
        import httpx
        from aegis_agent_worker.llm.client import OpenAICompatibleClient
        self.case, self.calls, self.quota = case, [], quota
        self.provider_name, self.model_name = settings.model_provider, settings.model_default_name
        self.latest_usage = None
        async def on_response(response):
            await response.aread()
            try:
                value = response.json()
                self.latest_usage = value.get("usage") if isinstance(value, dict) else None
            except ValueError:
                self.latest_usage = None
        self.http = httpx.AsyncClient(timeout=settings.model_request_timeout_seconds,
            transport=None if live else httpx.MockTransport(self.mock_http),
            event_hooks={"response": [on_response]})
        self.client = OpenAICompatibleClient(settings, self.http)
    def mock_http(self, request):
        import httpx
        action = self.case["mock"]
        if action == "network":
            return httpx.Response(503)
        if action in {"invalid", "repair"}:
            content = "not JSON" if action == "invalid" or len(self.calls) == 0 else '{"decision":"reply"}'
        elif action == "bypass":
            content = '{"decision":"reply","approved":true,"workflow":"mail_send_confirmation"}'
        elif action in {"card", "bad_target"}:
            if action == "card" and not self.case.get("_legacy") and len(self.calls) == 0:
                content = json.dumps(dict(action="read_context", tool_name="conversation.cards.get",
                                          arguments={"selector": "latest"}))
            elif action == "card" and self.case.get("card") != "valid":
                content = json.dumps(dict(decision="clarify", clarification_question="请重新选择邮件"))
            else:
                target = dict(resource_type="email", id=EMAIL, source_version="v1" if action == "card" else "fake")
                content = json.dumps(dict(decision="delegate", domain="mail", intent="query_mail",
                                          instruction="定位邮件", target=target))
        elif action in {"reply", "clarify"}:
            content = json.dumps(dict(decision=action, **({"clarification_question": "请明确任务"}
                                                        if action == "clarify" else {})))
        else:
            domain, intent = action.split(".")
            content = json.dumps(dict(decision="delegate", domain=domain, intent=intent, instruction="固定模拟任务"))
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})
    async def complete(self, messages):
        return await self.call(messages, False)
    async def complete_json(self, messages):
        return await self.call(messages, True)
    async def call(self, messages, json_mode):
        from aegis_agent_worker.harness.exceptions import HarnessError
        if self.quota[0] <= 0:
            raise HarnessError("EVAL_CALL_LIMIT", "评测请求额度耗尽")
        self.quota[0] -= 1
        self.latest_usage = None
        start, error = monotonic(), None
        try:
            return await (self.client.complete_json(messages) if json_mode else self.client.complete(messages))
        except Exception as exc:
            error = getattr(exc, "code", "MODEL_CALL_FAILED")
            raise
        finally:
            usage = self.latest_usage if isinstance(self.latest_usage, dict) else {}
            counts = {name: usage.get(name) if type(usage.get(name)) is int and 0 <= usage[name] <= 2**63-1 else None
                      for name in ("prompt_tokens", "completion_tokens", "total_tokens")}
            try:
                from aegis_agent_worker.llm.usage import current_model_purpose
                purpose = current_model_purpose(json_mode)
            except ImportError:
                purpose = None
            self.calls.append(dict(duration_ms=round((monotonic()-start)*1000, 3), error=error,
                                   purpose=purpose, **counts))


async def evaluate(case, args, quota, clock):
    from aegis_agent_worker.config.settings import Settings
    from aegis_agent_worker.agent.contracts.decision import ConversationCapability
    from aegis_agent_worker.agent.specialists.main_agent import MainAgent
    from aegis_agent_worker.agent.workflows.main.decision_workflow import MainDecisionWorkflow
    from aegis_agent_worker.harness.executor import HarnessExecutor
    from aegis_agent_worker.llm.guarded_client import GuardedLLMClient
    from aegis_agent_worker.repository.run_repository import ClaimedRun
    options = inspect.signature(MainDecisionWorkflow).parameters
    case = {**case, "_legacy": "context_tools" not in options}
    live = args.mode == "live" and case["scenario"] == "semantic"
    settings = (Settings(_env_file=args.env_file) if live else Settings(_env_file=None,
        database_url="postgresql://unused/unused", redis_url="redis://unused", model_provider="openai",
        model_api_base="https://eval.invalid/v1", model_api_key="offline", model_default_name="offline-fixture"))
    model = RecordingModel(case, settings, live, quota)
    budget, facts = MemoryBudget(), Facts(case)
    harness = HarnessExecutor(MemoryDatabase(), repository=budget)
    facts.harness = harness
    main = MainAgent(GuardedLLMClient(model, harness))
    capabilities = [ConversationCapability(domain=d, intent=i, workflow=w, description=desc)
                    for (d, i, w), desc in zip(CAPS, ["查看指定范围日程", "查询空闲时段",
                        "生成会议草稿并等待人工确认；不能修改现有草稿", "浏览或定位邮件操作卡片；摘要起草需点击"])]
    kwargs = {"context_tools": facts} if "context_tools" in options else {}
    graph = MainDecisionWorkflow(main, facts, capabilities, **kwargs).build()
    run = ClaimedRun(id=uuid4(), tenant_id=UUID(int=2), user_id=UUID(int=3), conversation_id=UUID(int=4),
                     run_type=case.get("run_type", "conversation"))
    state = dict(run_id=str(run.id), tenant_id=str(run.tenant_id), user_id=str(run.user_id),
                 conversation_id=str(run.conversation_id), run_type=run.run_type,
                 latest_user_message=case["input"], history=case.get("history", []))
    start, result, error, visits = monotonic(), {}, None, []
    try:
        import aegis_agent_worker.agent.specialists.main_agent as module
        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                value = datetime.fromisoformat(clock)
                return value.astimezone(tz) if tz else value.replace(tzinfo=None)
        with patch.object(module, "datetime", FixedDateTime):
            async with harness.scope(run):
                if case["scenario"] == "page":
                    result = await evaluate_page(main, facts, state, visits)
                else:
                    result = await graph.ainvoke(state)
    except Exception as exc:
        from aegis_agent_worker.llm.client import LLMError
        error = getattr(exc, "code", "LLM_UNAVAILABLE" if isinstance(exc, LLMError) else "EVAL_ERROR")
    finally:
        await model.http.aclose()
    decision = result.get("main_decision", {})
    actual = ("error" if error else ("page."+visits[0] if visits else
               (decision.get("domain", "")+"."+decision.get("intent", "")
                if decision.get("decision") == "delegate" else decision.get("decision"))))
    target = decision.get("target")
    target_ok = case.get("target", "none") != "valid" or bool(target and target.get("id") == EMAIL and target.get("source_version") == "v1")
    if case.get("target") == "none":
        target_ok = not target
    audit = facts.audits[-1] if facts.audits else {}
    candidates = audit.get("candidate_capabilities", [d+"."+i for d, i, _ in CAPS])
    relevant = [v for v in case["allowed"] if v in [d+"."+i for d, i, _ in CAPS]]
    illegal = (["forbidden_delegation"] if decision.get("decision") == "delegate" and
               (case.get("forbid_delegation") or actual not in {d+"."+i for d, i, _ in CAPS}) else [])
    # 这里只能证明路由沙箱的禁止交接；真实 ToolGateway／权限／审批零绕过由已有测试及端到端验证负责。
    error_ok = error is None or error == case.get("allowed_error", "LLM_UNAVAILABLE")
    return dict(case_id=case["id"], scenario=case["scenario"], entry=run.run_type, actual=actual,
        decision=decision.get("decision"), passed=actual in case["allowed"] and target_ok and error_ok,
        target_ok=target_ok, outcome=audit.get("outcome"), error=error,
        candidate_count=len(candidates), candidate_recall=bool(set(relevant) & set(candidates)) if relevant else None,
        candidate_capabilities=candidates, filter_reason_codes=audit.get("filter_reason_codes", []),
        validation_issue_codes=audit.get("validation_issue_codes", []),
        repair_count=audit.get("repair_count", max(0, len(model.calls)-1-len(facts.reads))),
        context_reads=len(facts.reads), duration_ms=round((monotonic()-start)*1000, 3),
        calls=model.calls, illegal_calls=illegal, external_business_calls=0,
        candidate_observation="audited" if "candidate_capabilities" in audit else "legacy_full_catalog",
        model_provider=model.provider_name, model_name=model.model_name,
        live_model=live, fixture_fingerprint=hashlib.sha256(json.dumps(case, sort_keys=True).encode()).hexdigest())


async def evaluate_page(main, facts, state, visits):
    from langgraph.graph import StateGraph, START, END
    from aegis_agent_worker.agent.graph.task_graph import TaskGraph, TaskGraphDependencies
    from aegis_agent_worker.agent.graph.state import TaskGraphState
    class Factory:
        def register(self, registry):
            for name in ("mail_extraction", "mail_reply_draft", "mail_send_confirmation", "mail_draft_revision"):
                def node(state, name=name):
                    visits.append(name)
                    return {"mail_workflow_completed": True}
                child = StateGraph(TaskGraphState)
                child.add_node("execute", node)
                child.add_edge(START, "execute")
                child.add_edge("execute", END)
                registry.register(name, child.compile())
    class Forbidden:
        async def load_history(self, *args):
            raise RuntimeError("页面任务不得加载对话")
        async def complete(self, *args):
            raise RuntimeError("页面任务不得生成普通回复")
    dependencies = TaskGraphDependencies(context_service=Forbidden(), lifecycle=object(), llm_client=Forbidden(),
        system_prompt="评测", workflow_factories=(Factory(),), main_agent=main, decision_context_service=facts)
    return await TaskGraph(dependencies).build().ainvoke(state)


async def run(args):
    data = load_cases(args.cases)
    cases = [c for c in data["cases"] if not args.case_id or c["id"] in args.case_id]
    if args.case_id and set(args.case_id) - {c["id"] for c in cases}:
        raise ValueError("未知 case-id")
    if args.limit:
        cases = cases[:args.limit]
    rows, quota = [], [args.max_model_calls]
    for repetition in range(args.repeat):
        for case in cases:
            row = await evaluate(case, args, quota, data["fixed_now"])
            rows.append({**row, "repetition": repetition+1})
            print(f"{case['id']}: {row['actual']} {'PASS' if row['passed'] else 'FAIL'}", flush=True)
            if quota[0] <= 0:
                break
        if quota[0] <= 0:
            break
    def git(*parameters):
        proc = subprocess.run(["git", "-C", str(args.worker_source), *parameters], capture_output=True, text=True)
        return proc.stdout.strip() if proc.returncode == 0 else "unavailable"
    report = dict(schema_version=1, mode=args.mode, label=args.label,
        dataset_version=data["version"], dataset_sha256=hashlib.sha256(args.cases.read_bytes()).hexdigest(),
        source_commit=args.source_revision or git("rev-parse", "HEAD"),
        source_dirty=False if args.source_revision else bool(git("status", "--porcelain", "--", "src")),
        source_revision_verified=bool(args.source_revision),
        source_sha256=hashlib.sha256(b"".join(
            path.relative_to(args.worker_source/"src").as_posix().encode()+path.read_bytes()
            for path in sorted((args.worker_source/"src").rglob("*.py")))).hexdigest(),
        fixed_now=data["fixed_now"], requested_runs=len(cases)*args.repeat, completed_runs=len(rows),
        complete=len(rows) == len(cases)*args.repeat and not any(r["error"] == "EVAL_CALL_LIMIT" for r in rows),
        summary=summarize(rows, args.mode), results=rows)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.output.exists():
        raise ValueError("输出文件已存在，避免覆盖历史评测")
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"报告：{args.output}")
    return 0 if report["complete"] and all(r["passed"] for r in rows) else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker-source", type=Path, default=ROOT)
    parser.add_argument("--source-revision", help="核验源码确实来自指定 Git commit，再标记报告")
    parser.add_argument("--cases", type=Path, default=ROOT/"tests/evaluation/data/main_routing_cases.json")
    parser.add_argument("--mode", choices=("mock", "live"), default="mock")
    parser.add_argument("--allow-live", action="store_true")
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--label", default="current")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--case-id", action="append")
    parser.add_argument("--max-model-calls", type=int, default=200)
    args = parser.parse_args()
    if args.repeat < 1 or args.max_model_calls < 1 or (args.limit is not None and args.limit < 1):
        parser.error("次数必须为正数")
    if args.mode == "live" and (not args.allow_live or not args.env_file):
        parser.error("真实模型需同时指定 --allow-live 和 --env-file")
    if args.output.exists():
        parser.error("报告已存在，请换一个输出路径")
    sys.path.insert(0, str(args.worker_source.resolve()/"src"))
    args.worker_source = args.worker_source.resolve()
    if not (args.worker_source/"src/aegis_agent_worker").is_dir():
        parser.error("worker-source 必须是含 src/aegis_agent_worker 的 Worker 目录")
    if args.source_revision:
        args.source_revision = verify_source_revision(args.worker_source, args.source_revision)
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
