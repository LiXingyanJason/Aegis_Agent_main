"""保守候选能力筛选：只缩小明显查询的领域，不替代LLM意图判断。"""
import re
from collections.abc import Sequence, Mapping
from dataclasses import dataclass
from typing import Any

from aegis_agent_worker.agent.contracts.decision import ConversationCapability


def capability_id(capability: ConversationCapability) -> str:
    return f"{capability.domain}.{capability.intent}"


@dataclass(frozen=True)
class CapabilitySelection:
    """只保存目录标识与稳定原因，不复制用户任务、引用标签或历史正文。"""
    candidate_ids: tuple[str, ...]
    reason_codes: tuple[str, ...]


class CapabilitySelector:
    """
    两层 Capability 决策处理：
        - CapabilitySelector：用保守规则(字符串匹配)筛选“哪些能力值得交给 LLM 考虑”，不确定时保留全部，不选择最终工作流。
        - 主 Agent MainDecisionWorkflow：由 LLM 判断最终领域、意图，或者选择澄清／普通回复。
    """
    policy_version = "capability-selector-v1"
    _signals = {
        "mail": re.compile(r"邮件|邮箱|收件箱|发件箱|\b(?:email|mail|inbox)\b", re.I),
        "calendar": re.compile(r"日历|日程|行程|空闲|可用时间|会议|开会|约会|\b(?:calendar|schedule|availability)\b", re.I),
    }
    _contextual = re.compile(r"那|这|第.{0,4}封|上次|之前|刚才|上述|它|重新|再|换|改")
    # 仅完整匹配最简单的浏览表达。自由文本、主题、附加条件等都保留全部能力。
    # “安排会议”不是查询表达，不能靠这条规则判断写入意图或审批。
    _simple_query = re.compile(
        r"(?:请|帮我|麻烦|我想|我要)?(?:查看|浏览|查询|列出|看看|打开)"
        r"(?:一下)?(?:我的|当前|今天|本周|明天)?"
        r"(?:邮件|邮箱|收件箱|日历|日程|行程|空闲时间|可用时间)(?:列表|情况|安排)?[。！？!?]*"
    )

    def select(
        self, capabilities: Sequence[ConversationCapability], message: str,
        verified_references: Sequence[Mapping[str, Any]] = (),
    ) -> CapabilitySelection:
        """输入只能是注册目录与服务端核验引用；不查连接、不调用工具、不决定权限。"""
        all_ids = tuple(capability_id(item) for item in capabilities)
        if not all_ids:
            return CapabilitySelection((), ("empty_catalog",))
        text = message.strip()
        signals = {domain for domain, pattern in self._signals.items() if pattern.search(text)}
        # 引用只使用类型，不扫描标题/正文；引用存在不等于当前任务属于该领域。
        reference_domains = {
            "mail" if item.get("resource_type") == "email" else "calendar"
            for item in verified_references
            if item.get("resource_type") in {"email", "calendar_draft"}
        }
        if self._contextual.search(text):
            reasons = ("context_dependent_keep_all",)
            if reference_domains:
                reasons += ("verified_reference_available",)
            return CapabilitySelection(all_ids, reasons)
        if len(signals) > 1:
            return CapabilitySelection(all_ids, ("multiple_domains_keep_all",))
        if not signals:
            return CapabilitySelection(all_ids, ("no_domain_signal_keep_all",))
        domain = next(iter(signals))
        if self._simple_query.fullmatch(text):
            candidates = tuple(capability_id(item) for item in capabilities if item.domain == domain)
            if candidates:
                # 保留该领域全部意图，最终意图仍由MainAgent决定。
                return CapabilitySelection(candidates, ("explicit_simple_query_domain",))
            return CapabilitySelection(all_ids, ("unregistered_domain_keep_catalog",))
        return CapabilitySelection(all_ids, ("uncertain_single_domain_keep_all",))
