"""Offline E09 objective metrics and anonymized review pack; no semantic score fabrication."""
import argparse
import json
from pathlib import Path
import random
from statistics import mean
from math import ceil


def summarize(folder):
    rows=[json.loads(line) for line in (folder/'results.jsonl').read_text(encoding='utf-8').splitlines()]
    arms={}
    for arm in 'ABC':
        group=[r for r in rows if r['arm']==arm]
        models=[a for r in group for a in r['attempts'] if a['kind']=='model']
        totals=[a['total_tokens'] for a in models if a['total_tokens'] is not None]
        durations=sorted(r['duration_seconds'] for r in group)
        arms[arm]=dict(tasks=len(group),generated=sum(r['status']=='generated' for r in group),
            model_requests=len(models),tool_attempts=sum(a['kind']=='tool' for r in group for a in r['attempts']),
            total_tokens_known=sum(totals),usage_unknown=len(models)-len(totals),
            mean_seconds=mean(durations),p95_seconds=durations[ceil(.95*len(durations))-1],
            repairs=sum(max(0,sum(a['kind']=='model' and a['purpose']!='skill_selection' for a in r['attempts'])-1) for r in group))
    selected=lambda row:any(b['status']=='selected' for b in row['bindings'])
    group=[r for r in rows if r['arm']=='C']
    tp=sum(r['expected_skill'] and selected(r) for r in group)
    fp=sum(not r['expected_skill'] and selected(r) for r in group)
    fn=sum(r['expected_skill'] and not selected(r) for r in group)
    tn=sum(not r['expected_skill'] and not selected(r) for r in group)
    report=dict(arms=arms,selection=dict(tp=tp,fp=fp,fn=fn,tn=tn,
        precision=tp/(tp+fp) if tp+fp else None,recall=tp/(tp+fn) if tp+fn else None,
        false_load_rate=fp/(fp+tn) if fp+tn else None),
        semantic_quality='pending independent review; generated does not mean correct',
        failed=[{k:r.get(k) for k in ('case_id','repeat','arm','error_code')} for r in rows if r['status']!='generated'])
    complete={}
    for row in rows: complete.setdefault(row['case_id'],{}).setdefault(row['repeat'],{})[row['arm']]=row
    # Only full paired observations; disclose exclusions.
    pairs=[v for repeats in complete.values() for v in repeats.values() if set(v)==set('ABC')]
    report['paired_observations']=len(pairs)
    rng=random.Random(20261025)
    ordered=rows.copy();rng.shuffle(ordered)
    pack=[];key=[]
    for i,row in enumerate(ordered):
        review_id=f'R{i+1:03d}'
        pack.append(dict(review_id=review_id,case_id=row['case_id'],output=row.get('output'),
            status=row['status'],checks_passed=None,critical_fact_error=None,notes=None))
        key.append(dict(review_id=review_id,case_id=row['case_id'],repeat=row['repeat'],arm=row['arm']))
    (folder/'summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    (folder/'blind_review.json').write_text(json.dumps(pack,ensure_ascii=False,indent=2),encoding='utf-8')
    (folder/'review_key.json').write_text(json.dumps(key,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('folder',type=Path)
    summarize(p.parse_args().folder)
