"""E08 actual repository SQL: 100 races and persisted boundary checks.

Minimal isolated tables, not a full migration/RLS/Redis verification.
"""
import argparse
import asyncio
import json
from pathlib import Path
from uuid import uuid4
from dataclasses import replace
from sqlalchemy import text
from experiment_storage import ExperimentStorage
from aegis_agent_worker.config.settings import get_settings
from aegis_agent_worker.repository.execution_repository import ExecutionRepository
from aegis_agent_worker.repository.run_repository import ClaimedRun


async def main(output):
    output.mkdir(parents=True,exist_ok=False)
    db=ExperimentStorage(get_settings(),'e08')
    report=dict(schema=db.schema,races=[],checks=[],rls_verified=False,redis_verified=False)
    path=output/'report.json'
    def save(): path.write_text(json.dumps(report,indent=2),encoding='utf-8')
    save();await db.create()
    repo=ExecutionRepository()
    async def create():
        run=ClaimedRun(uuid4(),uuid4(),uuid4(),None,'mail_reply_draft')
        async with db.session() as session:
            async with session.begin():
                await session.execute(text("INSERT INTO agent_runs VALUES(:id,:t,:u,'running')"),dict(id=run.id,t=run.tenant_id,u=run.user_id))
                assert await repo.ensure_budget(session,run,'experiment-v1')=='experiment-v1'
        return run
    async def reserve(run,**overrides):
        args=dict(kind='model',maximum=1,policy_version='experiment-v1',elapsed_ms=0,time_limit_ms=120000)
        args.update(overrides)
        async with db.session() as session:
            async with session.begin():
                return await repo.reserve_call(session,run,**args)
    try:
        for i in range(100):
            run=await create()
            gate=asyncio.Event()
            async def racer():
                await gate.wait()
                return await reserve(run)
            tasks=[asyncio.create_task(racer()) for _ in range(2)];gate.set()
            results=await asyncio.gather(*tasks)
            async with db.session() as session:
                snapshot=dict(await repo.snapshot(session,run))
            report['races'].append(dict(index=i,winners=sum(r is not None for r in results),model_calls=snapshot['model_calls']))
            assert sum(r is not None for r in results)==1 and snapshot['model_calls']==1
        for variant in ('wrong_user','wrong_tenant','wrong_policy','cancelled','waiting_input','waiting_confirmation','completed','time_exhausted'):
            run=await create();candidate=run;kw={}
            if variant=='wrong_user': candidate=replace(run,user_id=uuid4())
            elif variant=='wrong_tenant': candidate=replace(run,tenant_id=uuid4())
            elif variant=='wrong_policy': kw['policy_version']='other'
            elif variant=='time_exhausted': kw['elapsed_ms']=120000
            else:
                async with db.session() as session:
                    async with session.begin():
                        await session.execute(text('UPDATE agent_runs SET status=:status WHERE id=:id'),dict(status=variant,id=run.id))
            assert await reserve(candidate,**kw) is None
            async with db.session() as session: snapshot=await repo.snapshot(session,run)
            assert snapshot['model_calls']==0
            report['checks'].append(dict(case=variant,passed=True))
        run=await create();assert await reserve(run)==0
        # Fresh session ensures original version/counters persist across consumers.
        async with db.session() as session:
            async with session.begin():
                assert await repo.ensure_budget(session,run,'new-default')=='experiment-v1'
                assert (await repo.snapshot(session,run))['model_calls']==1
        assert await reserve(run) is None
        report['checks'].append(dict(case='resume_keeps_version_and_used_budget',passed=True))
        report['passed']=True;save()
    finally:
        await db.cleanup();report['schema_cleaned']=True;save()
    print('100 SQL races and 9 SQL boundary checks passed; isolated schema cleaned',flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True)
    asyncio.run(main(p.parse_args().output.resolve()))
