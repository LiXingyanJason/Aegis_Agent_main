"""团队站内草稿持久化/复核与审批准备，最终写入仍复用CalendarConfirmationService。"""
from contextlib import asynccontextmanager
from uuid import UUID, uuid5
from datetime import timezone

from aegis_agent_worker.agent.contracts.cooperation.calendar import validate_team_proposal
from aegis_agent_worker.agent.contracts.cooperation.mail import MailMeetingRequirements
from aegis_agent_worker.agent.contracts.decision import ResourceReference
from aegis_agent_worker.config.database import TenantContext, tenant_transaction
from aegis_agent_worker.repository.calendar_draft_repository import CalendarDraftRepository
from aegis_agent_worker.service.calendar.calendar_confirmation_service import _meeting_interval
from aegis_agent_worker.tool.contracts import ToolError
from aegis_agent_worker.event.cooperation_context import association


class CalendarDraftService:
    def __init__(self, database, runs, mail_sources=None, events=None, repository=None):
        self._database, self._runs, self._sources, self._events = database, runs, mail_sources, events
        self._repository = repository or CalendarDraftRepository()

    @asynccontextmanager
    async def _session(self, run):
        async with self._database.session() as session:
            async with tenant_transaction(session, TenantContext(run.tenant_id,run.user_id)):
                yield session

    async def verify_sources(self, run, references):
        if references and self._sources is None:
            raise ToolError("MAIL_SOURCE_VERIFIER_REQUIRED", "未配置邮件来源复核")
        for reference in references:
            await self._sources.describe(run, ResourceReference.model_validate(reference))

    async def _valid(self, run, reference):
        reference = ResourceReference.model_validate(reference)
        async with self._session(run) as session:
            row = await self._repository.load(session, run, reference.id)
        if (row is None or reference.resource_type != "calendar_draft" or reference.source_version is not None
                or row["version"] != reference.version
                or row["status"] not in {"draft","pending_approval"} or not row["readable"]
                or not isinstance(row.get("metadata"), dict)):
            raise ToolError("CALENDAR_DRAFT_CHANGED", "草稿不存在、版本变化、失效或已撤权")
        metadata=row["metadata"]
        if (metadata.get("draft_version")!=row["version"] or metadata.get("draft_snapshot")!=self._snapshot(row)
                or not isinstance(metadata.get("source_references"),list)):
            raise ToolError("CALENDAR_DRAFT_CHANGED","草稿内容与保存快照不一致")
        await self.verify_sources(run, row["metadata"]["source_references"])
        return row

    @staticmethod
    def _snapshot(row):
        return dict(title=row["title"],description=row["description"] or "",
            start_at=row["start_at"].astimezone(timezone.utc).isoformat(),
            end_at=row["end_at"].astimezone(timezone.utc).isoformat(),timezone=row["timezone"],
            attendees=sorted(row["attendees"]),connection_id=str(row["connection_id"]))

    async def find_saved(self, run, delegation, requirements, references):
        draft_id = uuid5(delegation.delegation_id, "calendar-draft")
        async with self._session(run) as session:
            existing = await self._repository.load(session, run, draft_id)
        if existing is None:
            return None
        ref = ResourceReference(resource_type="calendar_draft", id=draft_id, version=existing["version"])
        row = await self._valid(run, ref)
        if (row["metadata"]["requirements"] != requirements.model_dump(mode="json")
                or row["metadata"]["source_references"] != [item.model_dump(mode="json") for item in references]):
            raise ToolError("CALENDAR_DRAFT_CONFLICT", "已有草稿与当前委派要求不一致，拒绝覆盖")
        return ref

    async def save(self, run, *, delegation, requirements, references, proposal, candidates, connection_id):
        proposal, by_id = validate_team_proposal(requirements, proposal, candidates)
        await self.verify_sources(run, references)
        existing = await self.find_saved(run, delegation, requirements, references)
        if existing is not None:
            return existing
        selected = by_id[proposal.selected_slot_id]
        start, end = _meeting_interval(selected, requirements.duration_minutes)
        metadata = dict(draft_id=str(uuid5(delegation.delegation_id, "calendar-draft")),
            draft_version=1,delegation_id=str(delegation.delegation_id), requirements=requirements.model_dump(mode="json"),
            draft_snapshot=self._snapshot(dict(title=proposal.title,description=proposal.description,start_at=start,
                end_at=end,timezone=requirements.timezone,attendees=proposal.attendees,connection_id=connection_id)),
            source_references=[item.model_dump(mode="json") for item in references],
            recommendation=dict(selected_slot_id=proposal.selected_slot_id, reason=proposal.reason,
                scheduling_warning="仅核验当前用户日历；其他参会人空闲情况未核验。",
                alternative_slots=[dict(slot_id=key,start_at=_meeting_interval(by_id[key],requirements.duration_minutes)[0].isoformat(),
                    end_at=_meeting_interval(by_id[key],requirements.duration_minutes)[1].isoformat())
                    for key in proposal.alternative_slot_ids]))
        draft_id = UUID(metadata["draft_id"])
        async with self._session(run) as session:
            if not await self._repository.lock_run(session, run):
                raise ToolError("CALENDAR_RUN_CHANGED", "任务不再允许生成草稿")
            row = await self._repository.load(session, run, draft_id)
            if row is not None:
                if row.get("metadata") != metadata or row["version"] != 1 or row["status"] != "draft":
                    raise ToolError("CALENDAR_DRAFT_CONFLICT", "草稿重放冲突，拒绝覆盖")
                return ResourceReference(resource_type="calendar_draft", id=draft_id, version=1)
            if await self._repository.connection(session, run, connection_id) is None:
                raise ToolError("CALENDAR_CONNECTION_REQUIRED", "原查询日历连接已失效")
            await self._repository.insert(session, run=run, draft_id=draft_id, connection_id=connection_id,
                title=proposal.title, description=proposal.description, start_at=start, end_at=end,
                timezone=requirements.timezone, attendees=proposal.attendees, status="draft")
            event = await self._runs.append_event(session, run_id=run.id, tenant_id=run.tenant_id,
                event_type="cooperation_calendar_draft_saved", payload=metadata)
            if self._events:
                self._events.publish_after_commit(event)
        return ResourceReference(resource_type="calendar_draft", id=draft_id, version=1)

    async def verify_result(self, run, delegation, result):
        if (result.expert!="calendar" or delegation.target!="calendar" or result.delegation_id!=delegation.delegation_id):
            return False
        if result.status != "completed":
            return result.draft is None and not result.references
        if (result.expert != "calendar" or delegation.target != "calendar"
                or result.delegation_id != delegation.delegation_id or result.draft is None
                or result.draft.id != uuid5(delegation.delegation_id,"calendar-draft")):
            return False
        try:
            row = await self._valid(run, result.draft)
        except ToolError:
            return False
        return row["metadata"]["source_references"] == [item.model_dump(mode="json") for item in result.references]

    async def prepare_approval(self, run, reference):
        ref = ResourceReference.model_validate(reference)
        await self._valid(run, ref)
        async with self._session(run) as session:
            if not await self._repository.lock_run(session, run):
                raise ToolError("CALENDAR_RUN_CHANGED", "任务不再允许准备确认")
            row = await self._repository.load(session, run, ref.id)
            if (row is None or row["version"] != ref.version or not row["writable"]
                    or row["status"] not in {"draft","pending_approval"}
                    or row["metadata"]["draft_snapshot"]!=self._snapshot(row)):
                raise ToolError("CALENDAR_DRAFT_CHANGED", "草稿版本或日历写入权限不可用")
            approval = await self._repository.approval(session, run, ref.id, ref.version)
            if approval:
                if approval["status"] != "pending" or not approval["valid"]:
                    raise ToolError("CALENDAR_APPROVAL_CHANGED", "原确认项已处理或失效")
                approval_id = approval["id"]
            else:
                approval_id = await self._repository.create_approval(session, run=run, draft_id=ref.id, version=ref.version,
                    title=row["title"],start_at=row["start_at"],end_at=row["end_at"],attendees=row["attendees"],
                    timezone=row["timezone"],recommendation_preview=row["metadata"]["recommendation"])
            await self._repository.mark_pending(session, run, ref.id, ref.version)
        # 不提前改run状态或发布可批准事件；Checkpoint持久化后由Orchestrator标记ready。
        return dict(draft_id=str(ref.id), draft_version=ref.version, approval_item_id=str(approval_id),
                    awaiting_confirmation=True, approval_ready_required=True)

    async def validate_approval_resume(self, run, reference, approval_id):
        """审批等待期间也可能撤权/换版本；不重新运行任何模型或改写确认内容。"""
        ref = ResourceReference.model_validate(reference)
        # 写入已成功但节点Checkpoint尚未提交时，允许最终写服务返回真实已有执行结果。
        async with self._session(run) as session:
            completed = await self._repository.completed_execution(session, run, ref.id, approval_id)
        if completed is not None:
            return
        row = await self._valid(run, ref)
        if not row["writable"]:
            raise ToolError("CALENDAR_CONNECTION_REQUIRED", "日历写入权限已撤销")

    async def mark_ready(self, run, payload):
        ref = ResourceReference(resource_type="calendar_draft", id=payload["draft_id"], version=payload["draft_version"])
        row=await self._valid(run, ref)
        if not row["writable"]:
            raise ToolError("CALENDAR_CONNECTION_REQUIRED","日历写入权限已撤销")
        async with self._session(run) as session:
            await self._repository.lock_run(session, run)
            approval = await self._repository.approval(session, run, ref.id, ref.version)
            if (not approval or str(approval["id"]) != payload["approval_item_id"] or approval["status"] != "pending"
                    or not approval["valid"] or not await self._repository.mark_ready(session,run,approval["id"])):
                raise ToolError("CALENDAR_APPROVAL_CHANGED", "确认项无法就绪")
            event = await self._runs.append_event(session,run_id=run.id,tenant_id=run.tenant_id,
                event_type="approval_required", payload=dict(approval_item_id=str(approval["id"]),
                    resource_type="calendar_event_draft",resource_id=str(ref.id),title=approval["title"],
                    action="calendar.create_event",status="pending",preview=approval["preview_snapshot"],
                    cooperation=association(run.id,"approval")))
            if self._events:
                self._events.publish_after_commit(event)
            from aegis_agent_worker.repository.execution_repository import ExecutionRepository
            from aegis_agent_worker.service.cooperation.team_event_service import TeamEventService
            budget=await ExecutionRepository().snapshot(session,run)
            if budget is None:
                raise ToolError("EXECUTION_STATE_UNAVAILABLE","无法核验确认任务策略")
            await TeamEventService(self._database,self._runs,self._events).record(run,phase="awaiting_confirmation",
                round_no=0,policy_version=budget["policy_version"],association=association(run.id,"approval"),session=session)
