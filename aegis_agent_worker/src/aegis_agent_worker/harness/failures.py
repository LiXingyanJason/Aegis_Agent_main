"""稳定审计分类，不写入原始异常/Prompt，不把网络或取消视为格式修复。"""
import asyncio


def execution_error_code(error):
    from aegis_agent_worker.llm.client import LLMError, LLMOutputError
    if isinstance(error, asyncio.CancelledError):
        return "EXECUTION_CANCELLED"
    if isinstance(error, TimeoutError):
        return "EXECUTION_TIMEOUT"
    if getattr(error, "code", None):
        return error.code
    if isinstance(error, LLMOutputError):
        return "MODEL_OUTPUT_INVALID"
    if isinstance(error, LLMError):
        return "LLM_UNAVAILABLE"
    return "EXECUTION_FAILED"
