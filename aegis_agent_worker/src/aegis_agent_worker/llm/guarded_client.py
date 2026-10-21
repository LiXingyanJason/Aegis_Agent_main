"""模型调用接入Harness，负责输入输出边界；不含SQL或业务决策。"""
import re

from aegis_agent_worker.harness.exceptions import HarnessError
from aegis_agent_worker.harness.executor import HarnessExecutor
from aegis_agent_worker.llm.client import LLMMessage, OpenAICompatibleClient
from aegis_agent_worker.common.usage import TokenUsage
from aegis_agent_worker.llm.usage import current_model_purpose
from aegis_agent_worker.llm.memory_scope import current_memory_request
from aegis_agent_worker.harness.context import get_execution
from aegis_agent_worker.agent.contracts.memory import PERSONALIZATION_PURPOSES


class GuardedLLMClient:
    """统一所有模型调用预算，包括主路由、领域规划、格式修复和正文生成。"""

    def __init__(self, client: OpenAICompatibleClient, harness: HarnessExecutor) -> None:
        self._client, self._harness = client, harness

    @property
    def provider_name(self) -> str:
        return self._client.provider_name

    @property
    def model_name(self) -> str:
        return self._client.model_name

    async def complete(self, messages: list[LLMMessage]) -> str:
        return await self._complete(messages, json_mode=False)

    async def complete_json(self, messages: list[LLMMessage]) -> str:
        """格式修复和首次JSON请求都经过相同预算，不绕过Harness。"""
        return await self._complete(messages, json_mode=True)

    async def check_delegation_capacity(self):
        """复用Harness的只读预检，不新增预算账户或模型请求。"""
        await self._harness.check_remaining()

    async def complete_prepared(self, messages, prepare_messages):
        """纯消息工厂可使用本次已核验Bundle；复用通信、预算与审计而非预读SQL。"""
        return await self._complete(messages,json_mode=False,prepare_messages=prepare_messages)

    async def complete_json_prepared(self, messages, prepare_messages):
        """结构化决策复用同一记忆复核、通信、预算及审计边界。"""
        return await self._complete(messages,json_mode=True,prepare_messages=prepare_messages)

    async def _complete(self, messages: list[LLMMessage], *, json_mode: bool,prepare_messages=None) -> str:
        original_messages=messages
        # 输入大小上限校验
        if sum(len(item.content) for item in messages) > self._harness.policy.max_input_chars:
            raise HarnessError("MODEL_INPUT_TOO_LARGE", "模型上下文超过允许大小")
        # 提示注入启发式检测：拼接user消息，标记常见指令覆盖模式。
        # 中文：忽略 后0~12个字符再跟 系统/之前/以上/指令。
        # 命中只添加风险标记，不宣称启发式检测可以完全阻断提示注入。
        flags = []
        user_text = "\n".join(item.content for item in messages if item.role == "user")
        if re.search(
            r"忽略.{0,12}(系统|之前|以上|指令)|ignore.{0,20}(previous|system)", user_text, re.I,
        ):
            flags.append("instruction_override_pattern")
        # 占用模型调用次数预算并进入用量审计；字符限制不是Token扣费。
        async with self._harness.attempt("model", self.model_name, risk_flags=flags) as audit:
            purpose = current_model_purpose(json_mode)
            audit.update(purpose=purpose, model_provider=self.provider_name,
                         model_name=self.model_name)

            # 调用 LLM 前，为本次调用按需装配长期记忆，并留下审计记录

            # 使用with memory_call_scope( run, "conversation_reply", ):后complete中会判断是否有memory_call_scope
            # 读不到（None）：不走长期记忆筛选，正常调用 LLM
            # 读得到：再核对 purpose、任务身份和白名单；通过后才调用 prepare_call()。
            request = current_memory_request()
            bundle = None
            audit.update(memory_count=0, memory_selection_reason='purpose_denied')
            if request is not None and purpose in PERSONALIZATION_PURPOSES and request.purpose == purpose:
                runtime = get_execution()
                if (runtime.run.id, runtime.run.tenant_id, runtime.run.user_id) != (
                    request.run.id, request.run.tenant_id, request.run.user_id):
                    raise HarnessError('MEMORY_IDENTITY_MISMATCH', '记忆请求与当前任务不一致')
                if request.service is not None:
                    # 真正调用准备记忆
                    bundle, reason = await request.service.prepare_call(runtime.run, purpose=purpose,
                        task_text=request.task_text, remaining_chars=self._harness.policy.max_input_chars
                            - sum(len(item.content) for item in messages))
                    audit.update(memory_policy_version=bundle.policy_version,
                                 memory_selection_reason=reason)
                    if bundle.selection_report is not None:
                        audit.update(memory_selection_details=bundle.selection_report.model_dump(mode='json'))
                    if prepare_messages is not None:
                        messages=prepare_messages(bundle)
                    if bundle.items:
                        # 低优先级数据先于本轮明确要求；不拼成新的系统指令。
                        offset = next((index for index,item in enumerate(messages) if item.role!='system'),len(messages))
                        messages = [*messages[:offset],LLMMessage(role='user',content=request.service.format_bundle(bundle)),
                                    *messages[offset:]]
                else:
                    audit.update(memory_selection_reason='memory_service_unavailable')
            if prepare_messages is not None and bundle is None:
                messages=prepare_messages(None)
            if (sum(len(item.content) for item in messages)>self._harness.policy.max_input_chars
                    and bundle is not None and bundle.items):
                # 纯工厂可能因时区长度变化增加输入；审计前回退无记忆并同步重建时间语义。
                details=audit.get('memory_selection_details')
                if details is not None:
                    details['excluded_counts']['input_budget']=details['excluded_counts'].get('input_budget',0)+len(bundle.items)
                    maximum=get_execution().policy_bundle.memory.max_audit_items
                    details['excluded']=[*details['excluded'],*[
                        dict(memory_id=str(item.memory_id),version=item.version,reason='input_budget') for item in bundle.items]][:maximum]
                bundle=bundle.model_copy(update={'items':()})
                messages=prepare_messages(None) if prepare_messages is not None else original_messages
                audit.update(memory_selection_reason='memory_budget_exhausted')
            if sum(len(item.content) for item in messages) > self._harness.policy.max_input_chars:
                raise HarnessError('MODEL_INPUT_TOO_LARGE', '模型上下文超过允许大小')
            if bundle is not None and bundle.items:
                try:
                    # 在发起模型请求前记录本次实际使用的记忆及调用关联 审计。
                    await request.service.record_call(runtime.run, audit.attempt_id, bundle)
                except Exception:
                    audit.update(memory_selection_reason='memory_audit_failed')
                    raise
                audit.update(memory_count=len(bundle.items))
            reset_usage = getattr(self._client, "reset_usage", None)
            if reset_usage is not None:
                reset_usage()
            # json_mode=True，需要 JSON 输出。等价于
            # if json_mode:
            #     complete = self._client.complete_json   # 没有就退回 complete
            # else:
            #     complete = self._client.complete
            complete = (getattr(self._client, "complete_json", self._client.complete)
                        if json_mode else self._client.complete)
            try:
                result = await complete(messages)  # 真正调用模型
            finally:
                usage = getattr(self._client, "usage", None)
                if not isinstance(usage, TokenUsage):
                    usage = TokenUsage()
                audit.update(prompt_tokens=usage.prompt_tokens, completion_tokens=usage.completion_tokens,
                             total_tokens=usage.total_tokens)
                if bundle is not None and bundle.items:
                    await request.service.finish_call(runtime.run, audit.attempt_id, usage)
            if len(result) > self._harness.policy.max_output_chars:  # 输出大小校验
                raise HarnessError("MODEL_OUTPUT_TOO_LARGE", "模型输出超过允许大小")
            return result
