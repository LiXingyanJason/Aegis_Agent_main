"""Offline checks of frozen E07 evidence; no model, database or tool calls."""
import argparse
import json
import math
import random
from datetime import datetime
from pathlib import Path
from statistics import mean


def dt(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def summarize(folder):
    cases = {c['id']: c for c in json.loads((folder / 'dataset.json').read_text(encoding='utf-8'))}
    rows = [json.loads(line) for line in (folder / 'results.jsonl').read_text(encoding='utf-8').splitlines()]
    checked = []
    for row in rows:
        case = cases[row['case_id']]
        events = row['actual_events']
        previews = [e['payload']['preview'] for e in row['events'] if e['event_type'] == 'approval_required']
        receipts = [e['payload'] for e in row['events'] if e['event_type'] == 'approval_executed']
        source = any(t['tool_name'] == 'mail.messages.get' and t['output_summary'].get('provider_message_id') == case['expected_id'] for t in row['tools'])
        valid = len(events) == 1 and bool(previews) and source
        if valid:
            actual, preview = events[0], previews[-1]
            start, end = dt(actual['start_at']), dt(actual['end_at'])
            lower, upper = dt(case['day'] + 'T09:00:00+08:00'), dt(case['day'] + 'T12:00:00+08:00')
            valid = (lower <= start < end <= upper and (end-start).total_seconds() == case['duration']*60
                     and set(actual['attendees']) == set(case['attendees'])
                     and dt(preview['start_at']) == start and dt(preview['end_at']) == end
                     and set(preview['attendees']) == set(actual['attendees'])
                     and any(r.get('provider_resource_id') == actual['external_event_id'] for r in receipts)
                     and not any(start < dt(b['end_at']) and end > dt(b['start_at']) for b in case['calendar']))
        errors = sorted({e['payload']['error_code'] for e in row['events'] if e['payload'].get('error_code')})
        checked.append(dict(case_id=row['case_id'], repeat=row['repeat'], expected=row['expected'],
                            actual_event_count=len(events), verified_write=bool(valid), errors=errors,
                            draft_ok=row['draft_ok'], stop=row['stop']))
    writes = [r for r in checked if r['expected'] == 'write']
    no_writes = [r for r in checked if r['expected'] != 'write']
    groups = {key: [r['verified_write'] for r in writes if r['case_id'] == key] for key in sorted({r['case_id'] for r in writes})}
    rng = random.Random(20261025)
    values = list(groups.values())
    samples = sorted(mean(x for group in rng.choices(values, k=len(values)) for x in group) for _ in range(10000))
    models = [a for r in rows for a in r['attempts'] if a['kind'] == 'model']
    times = sorted(r['wall_seconds'] for r in rows)
    return dict(tasks=len(rows), expected_write=len(writes), verified_writes=sum(r['verified_write'] for r in writes),
                write_rate_cluster_bootstrap_95=[samples[249], samples[9749]],
                expected_no_write=len(no_writes), no_write_observed=sum(r['actual_event_count']==0 for r in no_writes),
                actual_events=sum(r['actual_event_count'] for r in checked),
                unverified_actual_writes=[r for r in checked if r['actual_event_count'] and not r['verified_write']],
                duplicate_event_runs=sum(r['actual_event_count']>1 for r in checked),
                raw_mechanical_pass=sum(r['mechanical_pass'] for r in rows),
                model_attempts=len(models), total_tokens_known=sum(a['total_tokens'] or 0 for a in models),
                usage_unknown=sum(a['total_tokens'] is None for a in models),
                tool_attempts=sum(a['kind']=='tool' for r in rows for a in r['attempts']),
                mean_wall_seconds=mean(times), p95_wall_seconds=times[math.ceil(len(times)*.95)-1],
                failures=[r for r in writes if not r['verified_write']], observations=checked,
                caveat='Mechanical no-write is not semantic success. Repeats clustered by template case; no independent quality review.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('directory', type=Path)
    args = parser.parse_args()
    result = summarize(args.directory)
    (args.directory / 'verified_summary.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k not in ('observations','failures')}, ensure_ascii=False, indent=2))
