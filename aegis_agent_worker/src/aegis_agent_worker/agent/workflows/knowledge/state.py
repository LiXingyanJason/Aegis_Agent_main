"""子图共享State；只保存安全摘录与引用，不保存原文或ACL。"""
from aegis_agent_worker.agent.graph.state import TaskGraphState


class KnowledgeQueryState(TaskGraphState, total=False):
    """字段在根图定义，以便子图结束后保留结构化结果。"""
