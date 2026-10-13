"""模型调用接入Harness，负责输入输出边界；不含SQL或业务决策。"""
import re

from aegis_agent_worker.harness.exceptions import HarnessError
from aegis_agent_worker.harness.executor import HarnessExecutor
from aegis_agent_worker.llm.client import LLMMessage, OpenAICompatibleClient


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

    async def _complete(self, messages: list[LLMMessage], *, json_mode: bool) -> str:
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
        # 占用模型预算并进入审计上下文
        async with self._harness.attempt("model", self.model_name, risk_flags=flags):
            # json_mode=True，需要 JSON 输出。等价于
            # if json_mode:
            #     complete = self._client.complete_json   # 没有就退回 complete
            # else:
            #     complete = self._client.complete
            complete = (getattr(self._client, "complete_json", self._client.complete)
                        if json_mode else self._client.complete)
            result = await complete(messages)  # 真正调用模型
            if len(result) > self._harness.policy.max_output_chars:  # 输出大小校验
                raise HarnessError("MODEL_OUTPUT_TOO_LARGE", "模型输出超过允许大小")
            return result
