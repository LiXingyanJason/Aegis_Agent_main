"""结构化日志格式测试。"""

import json
import logging
from uuid import UUID

from app.observability.structured_logging import JsonFormatter


def test_json_formatter_keeps_only_allowlisted_business_fields() -> None:
    """测试日志保留 run_id 等关联字段，不把调用方误传的 token 写入输出。"""
    logger = logging.getLogger("tests.observability")
    run_id = UUID("d99ce16b-7970-46b7-8d18-6b953fdfb5fa")
    record = logger.makeRecord(
        logger.name,
        logging.INFO,
        __file__,
        1,
        "agent_run_finished",
        (),
        None,
        extra={"event": "agent_run_finished", "run_id": run_id, "token": "must-not-log"},
    )

    payload = json.loads(JsonFormatter().format(record))

    assert payload["event"] == "agent_run_finished"
    assert payload["run_id"] == str(run_id)
    assert "token" not in payload
