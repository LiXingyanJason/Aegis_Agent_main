"""只提取校验类型和白名单路径，不使用异常msg/input/ctx。"""
import json

from pydantic import ValidationError

from aegis_agent_worker.agent.contracts.output_validation import OutputValidationIssue, SAFE_FIELDS


def safe_output_issues(error):
    if isinstance(error, ValidationError):
        issues = []
        for item in error.errors(include_input=False, include_context=False, include_url=False)[:8]:
            parts = [part if isinstance(part, str) and part in SAFE_FIELDS else "field"
                     for part in item["loc"] if not isinstance(part, int)]
            path = ".".join(parts[:8])
            try:
                issue = OutputValidationIssue(path=path, type="invalid_json" if item["type"] == "json_invalid" else item["type"])
            except ValueError:
                issue = OutputValidationIssue(path=path, type="schema_invalid")
            issues.append(issue)
        return tuple(issues)
    if getattr(error, "validation_issues", None):
        try:
            return tuple(OutputValidationIssue.model_validate(item) for item in error.validation_issues[:8])
        except (ValueError, TypeError):
            pass
    return (OutputValidationIssue(type="invalid_json" if isinstance(error, json.JSONDecodeError)
                                  else "invalid_output"),)
