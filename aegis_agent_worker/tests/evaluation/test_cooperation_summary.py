"""Evidence checks must reject malformed writes, not trust recorded pass flags."""
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('e07_summary', ROOT / 'scripts/summarize_cooperation_e2e.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.mark.parametrize('tamper', ['none', 'participants', 'receipt', 'source', 'duplicate', 'conflict'])
def test_frozen_success_is_rechecked(tmp_path, tamper):
    evidence = ROOT / '文档/阶段1/实验数据/20261025-e07-formal'
    case = json.loads((evidence / 'dataset.json').read_text(encoding='utf-8'))[0]
    row = json.loads((evidence / 'results.jsonl').read_text(encoding='utf-8').splitlines()[0])
    if tamper == 'participants':
        row['actual_events'][0]['attendees'] = []
    elif tamper == 'receipt':
        row['events'] = [e for e in row['events'] if e['event_type'] != 'approval_executed']
    elif tamper == 'source':
        row['tools'] = [t for t in row['tools'] if t['tool_name'] != 'mail.messages.get']
    elif tamper == 'duplicate':
        row['actual_events'] *= 2
    elif tamper == 'conflict':
        case['calendar'] = [row['actual_events'][0]]
    (tmp_path / 'dataset.json').write_text(json.dumps([case]), encoding='utf-8')
    (tmp_path / 'results.jsonl').write_text(json.dumps(row), encoding='utf-8')
    result = module.summarize(tmp_path)
    assert result['verified_writes'] == (1 if tamper == 'none' else 0)
    assert result['raw_mechanical_pass'] == 1  # Original flag cannot override independent checks.
