"""Frozen E04 candidate rerank ablation. Prepare offline; real run is opt-in.

Real run creates only a random local PostgreSQL schema for actual Harness budgets
and attempts; it does not search business data or connect to Qdrant. External
query/fictional snippets and fees require separate authorization.
"""
import argparse
import asyncio
from dataclasses import replace
from hashlib import sha256
import json
import random
from statistics import mean
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse
from uuid import uuid4
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine,async_sessionmaker
from contextlib import asynccontextmanager
from retrieval_eval.dataset import load_dataset,identifier
from retrieval_eval.metrics import ranking_metrics
from aegis_agent_worker.config.database import Database
from aegis_agent_worker.config.settings import get_settings
from aegis_agent_worker.harness.executor import HarnessExecutor
from aegis_agent_worker.harness.bootstrap import create_worker_policy_provider
from aegis_agent_worker.rerank.client import RerankClient
from aegis_agent_worker.rerank.executor import GovernedReranker
from aegis_agent_worker.tool.contracts import ToolContext

ROOT=Path(__file__).resolve().parents[1]


def summarize(results):
    rows=[r for r in results if r['split']=='test' and r['kind']=='answerable']
    if not rows:return dict(test_n=0)
    summary=dict(test_n=len(rows),baseline={k:mean(r['baseline'][k] for r in rows) for k in rows[0]['baseline']},
        rerank={k:mean(r['rerank'][k] for r in rows) for k in rows[0]['rerank']})
    groups={}
    for r in rows:groups.setdefault(r['query_id'].split('-')[0],[]).append(r['rerank']['recall']-r['baseline']['recall'])
    keys=sorted(groups);rng=random.Random(20261102)
    samples=[]
    for _ in range(2000):
        draws=rng.choices(keys,k=len(keys))
        samples.append(mean(v for key in draws for v in groups[key]))
    samples.sort()
    summary['recall_delta_cluster_ci95']=[samples[50],samples[1950]]
    summary['no_answer']=[dict(query_id=r['query_id'],returned_candidates=len(r['ranking'])) for r in results if r['kind']=='no_answer']
    return summary


def snapshot():
    data,queries,chunks=load_dataset(ROOT/'tests/evaluation/data/hybrid_retrieval_e04_v1.json')
    original=ROOT/'文档/阶段1/实验数据/20261022-e04/20261022-e04-verified.json'
    raw=original.read_bytes();report=json.loads(raw)
    if report['dataset_sha256']!=sha256((ROOT/'tests/evaluation/data/hybrid_retrieval_e04_v1.json').read_bytes()).hexdigest():
        raise ValueError('Frozen E04 dataset mismatch')
    by_key={c['key']:c for c in chunks};query_map={q['id']:q for q in queries}
    rows=[]
    for result in report['results']:
        if result['kind'] not in {'answerable','no_answer'}:continue
        # Reproduce stable RRF from the exact recorded lexical/vector ordering.
        scores={}
        for arm in ('lexical','vector'):
            for rank,c in enumerate(result['candidates'][arm],1):
                scores[c['chunk']]=scores.get(c['chunk'],0)+1/(report['policy']['rrf_k']+rank)
        keys=sorted(scores,key=lambda key:(-scores[key],identifier(key)))[:20]
        if any(not (key.startswith('D') or key=='C06.current') for key in keys):
            raise ValueError('Unexpected canary in rerank input')
        rows.append(dict(query_id=result['query_id'],split=result['split'],kind=result['kind'],
            query=query_map[result['query_id']]['query'],qrels=result['qrels'],
            candidate_keys=keys,documents=[by_key[k]['content'] for k in keys],
            reference_rankings=result['rankings']))
    return dict(version='rerank-frozen-e04-v1',source_sha256=sha256(raw).hexdigest(),rows=rows,
        limitations=['historical_short_synthetic_corpus','no_new_retrieval','no_answer_generation',
                     'not_phase2_chunking_quality_validation'])


class ExperimentalDatabase:
    def __init__(self,settings,schema):
        self.engine=create_async_engine(settings.database_async_url,hide_parameters=True,
            connect_args={'server_settings':{'search_path':schema+',pg_catalog'}})
        self.factory=async_sessionmaker(self.engine,expire_on_commit=False)
    @asynccontextmanager
    async def session(self):
        async with self.factory() as s:yield s


async def live(args):
    s=get_settings()
    if not args.allow_external or not args.allow_isolated_schema: raise ValueError('Explicit opt-ins required')
    if urlparse(s.database_async_url.replace('postgresql+asyncpg://','postgresql://')).hostname not in {'localhost','127.0.0.1','::1'}:
        raise ValueError('Local development PostgreSQL only')
    if not s.rerank_enabled or not s.rerank_external_allowed:raise ValueError('Rerank configuration/external permission required')
    prepared=snapshot();schema='aegis_rr_'+uuid4().hex
    admin=Database(s);db=ExperimentalDatabase(s,schema)
    made=False;report=dict(snapshot=prepared,resources=dict(schema=schema),complete=False,cleanup=False,results=[],
        provider='dashscope',model=s.rerank_model,endpoint_host=s.rerank_api_url.host,max_requests=54)
    try:
        async with admin.engine.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
            await conn.execute(text(f'CREATE TABLE "{schema}".agent_runs(id uuid PRIMARY KEY,tenant_id uuid,user_id uuid,status text)'))
            for table in ('run_execution_budgets','run_execution_attempts'):
                await conn.execute(text(f'CREATE TABLE "{schema}".{table} (LIKE public.{table} INCLUDING ALL)'))
            # Business database need not have 024 applied: add only to our clones.
            await conn.execute(text(f'ALTER TABLE "{schema}".run_execution_budgets ADD COLUMN IF NOT EXISTS rerank_calls integer NOT NULL DEFAULT 0'))
            await conn.execute(text(f'ALTER TABLE "{schema}".run_execution_attempts ADD COLUMN IF NOT EXISTS rerank_metadata jsonb'))
            await conn.execute(text(f'ALTER TABLE "{schema}".run_execution_attempts DROP CONSTRAINT IF EXISTS run_execution_attempts_kind_check'))
            await conn.execute(text(f'ALTER TABLE "{schema}".run_execution_attempts ADD CONSTRAINT run_execution_attempts_kind_check CHECK(kind IN (\'model\',\'tool\',\'rerank\'))'))
        made=True
        policies=create_worker_policy_provider(knowledge_enabled=True,rerank_enabled=True)
        harness=HarnessExecutor(db,policy_provider=policies)
        client=RerankClient(s);rerank=GovernedReranker(s,harness,client)
        tenant,user=uuid4(),uuid4()
        for row in prepared['rows']:
            run=SimpleNamespace(id=uuid4(),tenant_id=tenant,user_id=user,run_type='conversation')
            async with db.session() as session:
                async with session.begin():
                    await session.execute(text("INSERT INTO agent_runs VALUES(:id,:tenant,:user,'running')"),dict(id=run.id,tenant=tenant,user=user))
            evidence=[SimpleNamespace(chunk_id=identifier(k),version_id=identifier(k.split('.')[0]+'.version'),
                content_hash=sha256(doc.encode()).hexdigest(),excerpt=doc) for k,doc in zip(row['candidate_keys'],row['documents'])]
            async def fictional_authorized():return True  # Frozen synthetic export, NOT business ACL verification.
            async with harness.scope(run):
                scores=await rerank.rank(ToolContext(tenant,user,run.id),row['query'],evidence,fictional_authorized)
            ordering=[row['candidate_keys'][i] for i,_ in scores][:5]
            report['results'].append(dict(query_id=row['query_id'],run_id=str(run.id),kind=row['kind'],split=row['split'],
                baseline=ranking_metrics(row['candidate_keys'][:5],row['qrels']) if row['qrels'] else None,
                rerank=ranking_metrics(ordering,row['qrels']) if row['qrels'] else None,
                reference_metrics={arm:ranking_metrics(ids,row['qrels']) for arm,ids in row['reference_rankings'].items()} if row['qrels'] else None,
                ranking=ordering,scores=[dict(index=i,relevance_score=v) for i,v in scores],
                reference_rankings=row['reference_rankings']))
        report['summary']=summarize(report['results'])
        report['complete']=True
    finally:
        if made:
            async with db.session() as session:
                audits=(await session.execute(text('SELECT * FROM run_execution_attempts ORDER BY started_at,id'))).mappings().all()
                report['audit']=[dict(r) for r in audits]
        # Exclusive report written BEFORE exact-schema cleanup. On failure retain
        # resources for diagnosis rather than delete unarchived evidence.
        args.output.parent.mkdir(parents=True,exist_ok=True)
        with args.output.open('x',encoding='utf-8') as f:json.dump(report,f,ensure_ascii=False,indent=2,default=str)
        if made and report['complete']:
            async with admin.engine.begin() as conn:await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            # Save a separate cleanup receipt; never rewrite frozen results.
            with args.output.with_suffix('.cleanup.json').open('x',encoding='utf-8') as f:
                json.dump(dict(schema=schema,cleanup=True),f)
        await db.engine.dispose();await admin.dispose()


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--live',action='store_true')
    p.add_argument('--allow-external',action='store_true')
    p.add_argument('--allow-isolated-schema',action='store_true')
    args=p.parse_args()
    if args.output.exists():raise ValueError('Report already exists')
    if args.live:asyncio.run(live(args))
    else:
        frozen=snapshot()
        args.output.parent.mkdir(parents=True,exist_ok=True)
        with args.output.open('x',encoding='utf-8') as f:json.dump(frozen,f,ensure_ascii=False,indent=2)
