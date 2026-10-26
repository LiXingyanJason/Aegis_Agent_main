"""Secondary deterministic constraint checks, not full semantic correctness.

Rules implement explicit exact-text/sentence/paragraph constraints in frozen data.
Subset reporting was chosen after inspecting first-round outputs: exploratory.
"""
import argparse
import json
import re
from pathlib import Path


def compact(value):
    return re.sub(r'[\s。.!！?？,，、：:;；“”"「」]','',value)


def check(case_id,body):
    if case_id=='S17': return compact(body)=='附件已收到'
    if case_id=='S22': return compact(body)=='续约资料附件已收到'
    if case_id=='S23': return compact(body)=='已收到续约会议照片照片很清晰'
    if case_id=='S25':
        sentences=[s for s in re.split(r'[。！？!?]+',body) if s.strip()]
        return len(sentences)==1 and '现场支持' in body
    if case_id=='S29':
        sentences=[s for s in re.split(r'[。！？!?]+',body) if s.strip()]
        return len(sentences)==2 and '尚未同意' in body and '响应时效' in body and '故障处理范围' in body
    if case_id=='S30':
        return len([p for p in body.splitlines() if p.strip()])==1 and '一年' in body and '7万元' in body and '合同' in body
    return None


def main(folder):
    rows=[json.loads(line) for line in (folder/'results.jsonl').read_text(encoding='utf-8').splitlines()]
    scored=[]
    for row in rows:
        output=row.get('output','')
        body=output.get('body','') if isinstance(output,dict) else output
        score=check(row['case_id'],body)
        if score is not None:scored.append(dict(case_id=row['case_id'],repeat=row['repeat'],arm=row['arm'],passed=score,body=body))
    result=dict(metric='explicit literal/sentence/paragraph constraints only',scope='exploratory subset; not overall guide quality',
        scores=scored,arms={a:dict(passed=sum(r['passed'] for r in scored if r['arm']==a),total=sum(r['arm']==a for r in scored)) for a in 'ABC'})
    (folder/'constraint_scores.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result['arms']))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('folder',type=Path);main(p.parse_args().folder)
