"""只读调用追溯，归属前置；历史事实与当前可用性分开，不输出旧正文。"""
import json
from app.service.run_service import RunService
from app.repository.memory_usage_repository import MemoryUsageRepository
from app.vo.run_context_vo import RunContextSummaryVO,MemoryAttemptVO,MemoryUseVO

REASONS=frozenset({'not_relevant','purpose_type_denied','entity_key_denied','invalid_record','entity_preferred',
    'summary_conflict','ambiguous_summary','duplicate_memory','item_budget','char_budget','byte_budget','input_budget','recall_limit'})


class MemoryContextSummaryService:
    def __init__(self,runs=None,repository=None):
        self._runs=runs or RunService()
        self._repository=repository or MemoryUsageRepository()

    async def get(self,session,user,run_id,limit=50):
        if not await self._runs.ensure_run_owned(session,user,run_id): return None
        rows=await self._repository.attempts(session,user,run_id,limit)
        truncated=len(rows)>limit;rows=rows[:limit]
        usages=await self._repository.usages(session,user,run_id,[row['id'] for row in rows],50)
        legacy=await self._repository.usages(session,user,run_id,[],limit+1,legacy=True)
        by_attempt={}
        for item in usages: by_attempt.setdefault(item['attempt_id'],[]).append(self._use(item))
        attempts=[]
        for row in rows:
            details=row.get('memory_selection_details') or {}
            if isinstance(details,str):
                try: details=json.loads(details)
                except ValueError: details={}
            if not isinstance(details,dict): details={}
            # 不原样回传审计JSON，避免未来新增字段带入模型文本/候选正文。
            raw_counts=details.get('excluded_counts')
            counts={key:value for key,value in raw_counts.items() if key in REASONS and type(value) is int and 0<=value<=500} if isinstance(raw_counts,dict) else {}
            version=details.get('ranking_policy_version')
            attempts.append(MemoryAttemptVO(attempt_id=row['id'],purpose=row.get('purpose'),status=row['status'],started_at=row['started_at'],
                memory_count=row.get('memory_count'),memory_policy_version=row.get('memory_policy_version'),
                selection_reason=row.get('memory_selection_reason'),ranking_policy_version=version if isinstance(version,str) and len(version)<=64 else None,
                recall_truncated=details.get('recall_truncated') is True,excluded_counts=counts,
                needs_review=bool(counts.get('summary_conflict') or counts.get('ambiguous_summary')),
                prompt_tokens=row.get('prompt_tokens'),completion_tokens=row.get('completion_tokens'),total_tokens=row.get('total_tokens'),
                memories=by_attempt.get(row['id'],[])))
        return RunContextSummaryVO(run_id=run_id,attempts=attempts,legacy_memories=[self._use(item) for item in legacy[:limit]],
            truncated=truncated or len(legacy)>limit)

    @staticmethod
    def _use(item):
        available=item['current_status']=='available'
        return MemoryUseVO(**{key:item.get(key) for key in ('memory_id','memory_version','memory_type','source_kind','included_in_prompt','selection_reason','current_status')},
            source_object_id=item.get('source_object_id') if available else None,
            source_version=item.get('source_version') if available else None)
