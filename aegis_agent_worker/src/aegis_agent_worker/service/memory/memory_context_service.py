"""长期记忆的确定性筛选与受控提示词装配。"""

import re
import json
from dataclasses import dataclass
from uuid import UUID
from datetime import datetime, timezone
from collections import Counter
from sqlalchemy.exc import SQLAlchemyError, NoResultFound
from pydantic import ValidationError, TypeAdapter

from aegis_agent_worker.agent.contracts.memory import MemoryContextBundle, MemoryContextItem, MemorySourceRef, MemoryEntity
from aegis_agent_worker.agent.contracts.memory import MemorySelectionReport,MemorySelectionIssue
from aegis_agent_worker.service.memory.selection import recall_hints,describe_candidate,rank_and_resolve
from aegis_agent_worker.harness.exceptions import HarnessError
from aegis_agent_worker.harness.policy_provider import DEFAULT_POLICIES

from aegis_agent_worker.config.database import Database, TenantContext, tenant_transaction
from aegis_agent_worker.repository.memory_repository import MemoryRepository


_WORD_PATTERN = re.compile(r"[\u4e00-\u9fff]{2,}|[a-zA-Z0-9_]{2,}")


@dataclass(frozen=True, slots=True)
class SelectedMemory:
    """已通过确定性相关性规则、可加入一次任务上下文的记忆。"""

    id: UUID
    content: str
    source: str


class MemoryContextService:
    """在不依赖 LLM 判断的前提下选择少量普通用户偏好。"""

    def __init__(self, database: Database, repository: MemoryRepository | None = None, policy_provider=None) -> None:
        self._database = database
        self._repository = repository or MemoryRepository()
        self._policies = policy_provider or DEFAULT_POLICIES

    async def prepare_call(self, run, *, purpose, task_text, remaining_chars):
        """每次实际调用 LLM 前，从数据库挑出本次可以使用的记忆，组成一个有上限的 MemoryContextBundle(此函数只装配数据库记忆，不包含redis)"""
        policy = self._policies.current().memory
        base = dict(tenant_id=run.tenant_id, user_id=run.user_id, run_id=run.id,
                    conversation_id=run.conversation_id, purpose=purpose,
                    policy_version=policy.version_for(purpose), selected_at=datetime.now(timezone.utc))
        def empty(reason):
            return MemoryContextBundle(**base,selection_report=MemorySelectionReport(
                ranking_policy_version=policy.ranking_version,candidates_considered=0)), reason
        if not policy.types_for(purpose):
            return empty('purpose_denied')
        if purpose=='calendar_planning':
            from aegis_agent_worker.agent.contracts.calendar_context import explicit_calendar_timezone
            if explicit_calendar_timezone(task_text) is not None:
                return empty('explicit_timezone_override')
        # 未部署可信tokenizer，不使用字符换算Token；配置Token硬上限时保守不注入。
        if policy.max_estimated_tokens is not None:
            return empty('token_estimate_unknown')
        if remaining_chars <= 0:
            return empty('input_budget_exhausted')
        context = TenantContext(tenant_id=run.tenant_id, user_id=run.user_id)
        hints=recall_hints(purpose,task_text,policy)
        try:
            async with self._database.session() as session:
                async with tenant_transaction(session, context):
                    # 从用户在这个“长期记忆”页面保存的记录中的全部条目。同时包含“长期摘要”和“实体记忆”
                    recall=getattr(self._repository,'recall_candidates',None)
                    if recall is not None:
                        candidates=await recall(session,tenant_id=run.tenant_id,user_id=run.user_id,hints=hints,policy=policy)
                    else:
                        # 仅兼容旧只读Repository测试替身；生产MemoryRepository始终使用权限前置排名SQL。
                        candidates=await self._repository.list_active_non_sensitive(
                            session,tenant_id=run.tenant_id,user_id=run.user_id,limit=policy.candidate_limit)
        except (SQLAlchemyError, OSError, TimeoutError):
            return empty('memory_unavailable')
        truncated=len(candidates)>policy.candidate_limit
        candidates=candidates[:policy.candidate_limit]
        prepared=[]; issues=[];counts=Counter()
        def exclude(row,reason):
            counts[reason]+=1
            if len(issues)<policy.max_audit_items:
                try: issues.append(MemorySelectionIssue(memory_id=row['id'],version=row['version'],reason=reason))
                except (ValueError,KeyError): pass
        for row in candidates:
            # 检查 用途 是否允许 该类型的记忆
            if row.get('memory_type') not in policy.types_for(purpose):
                exclude(row,'purpose_type_denied')
                continue
            try:
                if row['memory_type']=='entity':
                # 如果是实体记忆，先把数据库中的 JSON 字符串（若有）解析成对象，再用 MemoryEntity 校验
                    entity=row['entity_value']
                    if isinstance(entity,str): entity=json.loads(entity)
                    entity=TypeAdapter(MemoryEntity).validate_python(entity).model_dump(mode='json')
                    if entity['key'] not in policy.entity_keys_for(purpose):
                        exclude(row,'entity_key_denied')
                        continue
                    row={**row,'entity_value':entity}
                # 判断与当前任务是否相关，使用确定性的词项和少量领域规则。
                # 日历用途只接收白名单结构字段，不以自由文本说明推断偏好或日期。
                candidate=describe_candidate(row,hints,policy)
                if candidate is None:
                    exclude(row,'not_relevant')
                    continue
                prepared.append(candidate)
            except (ValidationError,ValueError,TypeError,KeyError):
                exclude(row,'invalid_record')
        ranked,conflicts=rank_and_resolve(prepared)
        for candidate,reason in conflicts: exclude(candidate['row'],reason)
        items=[]
        for candidate in ranked:
            row=candidate['row']
            if len(items)>=policy.item_limit_for(purpose):
                exclude(row,'item_budget');continue
            try:
                # 取得安全摘录，控制单条长度
                content = candidate['content'][:policy.max_item_chars]
                if row['memory_type']=='entity' and len(candidate['content'])>policy.max_item_chars:
                    exclude(row,'char_budget');continue
                manual=row['source_kind']=='manual'
                source = MemorySourceRef(kind=row['source_kind'],
                    object_id=row['id'] if manual else row['source_object_id'],
                    version=str(row['version']) if manual else row['source_version'])
                item = MemoryContextItem(memory_id=row['id'], tenant_id=run.tenant_id,
                    user_id=run.user_id, version=row['version'], memory_type=row['memory_type'],
                    source=source, excerpt=content, selection_reason='confirmed_summary'
                        if row['memory_type']=='long_term_summary' else 'relevant_confirmed_preference')
                # 试着加入，并检查总预算
                bundle = MemoryContextBundle(**base, items=tuple([*items, item]))
                if bundle.content_chars>policy.max_total_chars:
                    exclude(row,'char_budget');continue
                if bundle.content_bytes>policy.max_content_bytes:
                    exclude(row,'byte_budget');continue
                if len(self.format_bundle(bundle))>remaining_chars:
                    exclude(row,'input_budget');continue
                items.append(item)
            except (ValidationError, ValueError, TypeError, KeyError):
                # 坏记录不向日志输出正文，也不扩展格式修复/模型重试。
                exclude(row,'invalid_record')
        conflict_count=sum(counts[key] for key in ('entity_preferred','summary_conflict','ambiguous_summary'))
        budget_count=sum(counts[key] for key in ('item_budget','char_budget','byte_budget','input_budget'))
        reason=('selected_with_exclusions' if conflict_count else 'selected_limited' if budget_count else 'selected') if items else (
            'memory_conflict_excluded' if conflict_count else 'memory_budget_exhausted' if budget_count else 'no_relevant_memory')
        report=MemorySelectionReport(ranking_policy_version=policy.ranking_version,candidates_considered=len(candidates),
            recall_truncated=truncated,excluded=tuple(issues),excluded_counts=dict(counts))
        return MemoryContextBundle(**base,items=tuple(items),selection_report=report),reason

    @staticmethod
    def format_bundle(bundle):
        from aegis_agent_worker.service.memory.memory_boundary import format_memory_data
        return format_memory_data(bundle)

    async def record_call(self, run, attempt_id, bundle):
        """调用前提交安全元数据；审计失败则不发送带记忆的模型请求。"""
        async with self._database.session() as session:
            async with tenant_transaction(session, TenantContext(tenant_id=run.tenant_id,user_id=run.user_id)):
                try:
                    await self._repository.record_bundle_usage(session, run=run, attempt_id=attempt_id, bundle=bundle)
                except NoResultFound:
                    raise HarnessError('MEMORY_CONTEXT_CHANGED','记忆或来源已变化，请重新执行任务') from None

    async def finish_call(self, run, attempt_id, usage):
        async with self._database.session() as session:
            async with tenant_transaction(session, TenantContext(tenant_id=run.tenant_id,user_id=run.user_id)):
                await self._repository.finish_bundle_usage(session, run=run, attempt_id=attempt_id, usage=usage)

def _terms(value: str) -> set[str]:
    """提取中文双字片段与英文/数字词，避免中文整句无法匹配。"""
    terms: set[str] = set()
    for value_part in _WORD_PATTERN.findall(value):
        if re.fullmatch(r"[\u4e00-\u9fff]+", value_part):
            terms.update(value_part[index : index + 2] for index in range(len(value_part) - 1))
        else:
            terms.add(value_part.lower())
    return terms


def _select_relevant(candidates: list[dict], task_terms: set[str], *, policy=None) -> list[SelectedMemory]:
    """按词块交集、更新时间顺序和长度上限选择候选，不调用模型做检索。"""
    selected: list[SelectedMemory] = []
    policy = policy or DEFAULT_POLICIES.current().memory
    total_chars = 0
    for candidate in candidates:
        entity=candidate.get('entity_value')
        if isinstance(entity,str): entity=json.loads(entity)
        # 实体值来自受Schema约束的字段，说明文字不能覆盖真正的时区/邮箱。
        content=(json.dumps(entity,ensure_ascii=False,sort_keys=True)
                 if candidate.get('memory_type')=='entity' and entity else str(candidate['content'])).strip()
        entity_relevant=bool(entity and (
            (entity.get('key')=='reply_style' and task_terms & {'邮件','回复','起草'}) or
            (entity.get('key')=='meeting_preferences' and task_terms & {'日程','会议','安排','时间','空闲','日期'}) or
            (entity.get('key')=='timezone' and task_terms & {'日程','会议','安排','时间','空闲','日期'})))
        if not content or not (entity_relevant or _is_relevant(content, task_terms)):
            continue
        if entity and len(content)>policy.max_item_chars:
            # 不截断邮箱/时区等结构化值，超额宁可不选。
            continue
        clipped = content[:policy.max_item_chars]
        if total_chars + len(clipped) > policy.max_total_chars:
            continue
        selected.append(
            SelectedMemory(id=candidate["id"], content=clipped, source=str(candidate["source"]))
        )
        total_chars += len(clipped)
        if len(selected) >= policy.max_items:
            break
    return selected


def _is_relevant(content: str, task_terms: set[str]) -> bool:
    """使用词块交集，并对时间安排类偏好提供明确的确定性规则。"""
    memory_terms = _terms(content)
    if memory_terms & task_terms:
        return True
    calendar_task_terms = {"日程", "会议", "安排", "空闲", "今天", "明天", "时间", "日期", "周一", "周二", "周三", "周四", "周五"}
    calendar_memory_terms = {"时区", "北京", "时间", "会议", "日程", "上午", "下午", "周一", "周二", "周三", "周四", "周五"}
    return bool(task_terms & calendar_task_terms and memory_terms & calendar_memory_terms)
