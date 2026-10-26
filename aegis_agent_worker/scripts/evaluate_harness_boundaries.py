"""Run the existing governance evidence baseline; no live model or business writes.

This baseline does not pretend to implement all 48 preregistered E08 cases.
It preserves each pytest result and explicitly marks missing experimental layers.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
FILES = {
    'budget_and_repair': 'tests/agent/test_harness_react_revision.py',
    'timeouts_and_audit': 'tests/agent/test_harness_layers.py',
    'cooperation_governance': 'tests/agent/test_cooperation_governance.py',
    'policy_pinning': 'tests/agent/test_policy_provider.py',
    'approval_and_write': 'tests/service/test_calendar_confirmation_service.py',
    'skill_lifecycle': 'tests/agent/test_skills.py',
    'input_choices': 'tests/agent/test_input_choices.py',
}


def summarize(path):
    cases = []
    for item in ET.parse(path).iter('testcase'):
        status = next((name for name in ('failure', 'error', 'skipped') if item.find(name) is not None), 'passed')
        cases.append(dict(node=item.get('classname') + '::' + item.get('name'),
                          status=status, duration_seconds=float(item.get('time', 0))))
    return cases


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    report = dict(experiment='E08-existing-regression-baseline',
                  started_at=datetime.now(timezone.utc).isoformat(),
                  live_model_requests=0, storage='test doubles',
                  preregistered_48_case_matrix_complete=False,
                  sql_atomicity_verified=False, full_api_e2e_verified=False,
                  layers_not_run=['isolated PostgreSQL/Redis', 'MCP response-loss injection'], groups=[])
    report['commit'] = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    report['source_sha256'] = hashlib.sha256(b''.join(
        str(p.relative_to(ROOT)).encode()+b'\0'+p.read_bytes()
        for p in sorted((ROOT/'src').rglob('*.py')))).hexdigest()
    for group, relative in FILES.items():
        xml = output / (group+'.xml')
        command = [sys.executable, '-m', 'pytest', relative, '-q', '-p', 'no:cacheprovider', '--junitxml='+str(xml)]
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=180)
        (output/(group+'.log')).write_text(result.stdout+result.stderr, encoding='utf-8')
        cases = summarize(xml) if xml.exists() else []
        report['groups'].append(dict(group=group, file=relative,
            file_sha256=hashlib.sha256((ROOT/relative).read_bytes()).hexdigest(),
            returncode=result.returncode, cases=cases))
        (output/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(group, result.returncode, len(cases), flush=True)
    cases = [case for group in report['groups'] for case in group['cases']]
    report['counts'] = {status: sum(c['status']==status for c in cases) for status in ('passed','failure','error','skipped')}
    report['finished_at'] = datetime.now(timezone.utc).isoformat()
    (output/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report['counts']), flush=True)
    return int(any(group['returncode'] for group in report['groups']))


if __name__ == '__main__':
    sys.exit(main())
