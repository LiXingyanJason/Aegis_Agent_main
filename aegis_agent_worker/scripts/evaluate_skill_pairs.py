"""Authorized E09 paired live experiment using actual Skill/SQL/Harness code."""
import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import random
import subprocess
import sys
from time import monotonic
from uuid import uuid4

import httpx
from sqlalchemy import text
from experiment_storage import ExperimentStorage
from aegis_agent_worker.config.settings import get_settings
from aegis_agent_worker.config.database import tenant_transaction,TenantContext
from aegis_agent_worker.agent.specialists.mail_agent import MailAgent
from aegis_agent_worker.agent.skills.contracts import SkillReadRequest
from aegis_agent_worker.harness.context import get_execution
from aegis_agent_worker.harness.executor import HarnessExecutor
from aegis_agent_worker.harness.policy_provider import LEGACY_POLICY,with_skills,PolicyProvider
from aegis_agent_worker.llm.client import OpenAICompatibleClient
from aegis_agent_worker.llm.guarded_client import GuardedLLMClient
from aegis_agent_worker.repository.run_repository import ClaimedRun
from aegis_agent_worker.repository.skill_repository import SkillRepository
from aegis_agent_worker.service.skills.skill_context_service import SkillContextService
from aegis_agent_worker.tool.contracts import ToolDefinition,ToolInvocation
from aegis_agent_worker.tool.gateway import ToolGateway
from aegis_agent_worker.tool.registry import ToolRegistry

ROOT=Path(__file__).resolve().parents[1]


class FixedGuide(SkillContextService):
    async def ensure_selection(self,client,messages,purpose):
        if not self.allowed(purpose): return
        run=get_execution().run
        claimed,binding=await self.repo.claim_selection(run,purpose)
        if not claimed:
            await self.checked(run,purpose)
            return
        candidates=await self.repo.candidates(run,purpose,20)
        assert len(candidates)==1
        chosen=candidates[0]
        await self.repo.select(run,purpose,chosen,'experiment_fixed_guide')
        async with self.repo.session(run) as session:
            prepared=await self.gateway.prepare(session,tenant_id=run.tenant_id,user_id=run.user_id,
                run_id=run.id,invocation=ToolInvocation('skills.read',dict(skill_id=chosen['id'],purpose=purpose)))
        await self.gateway.invoke(prepared)


async def main(args):
    output=args.output.resolve(); output.mkdir(parents=True,exist_ok=False)
    dataset=json.loads(args.dataset.read_text(encoding='utf-8'))
    settings=get_settings()
    storage=ExperimentStorage(settings,'e09')
    meta=dict(experiment='E09',status='preparing',schema=storage.schema,
        started_at=datetime.now(timezone.utc).isoformat(),model=settings.model_default_name,
        provider=settings.model_provider,endpoint_host=settings.model_api_base.host,
        dataset_sha256=hashlib.sha256(args.dataset.read_bytes()).hexdigest(),request_limit=args.request_limit,
        commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        source_sha256=hashlib.sha256(b''.join(str(p.relative_to(ROOT)).encode()+p.read_bytes() for p in sorted((ROOT/'src').rglob('*.py')))).hexdigest(),
        annotation='author rubric; outputs pending blinded review',full_schema_migration=False,
        storage='minimal support tables + actual migration022; actual repositories; no RLS isolation claim')
    def save(): (output/'metadata.json').write_text(json.dumps(meta,ensure_ascii=False,indent=2),encoding='utf-8')
    save()
    await storage.create()
    count=0
    async def limit(request):
        nonlocal count
        if count>=args.request_limit: raise RuntimeError('EXPERIMENT_REQUEST_LIMIT')
        count+=1
        meta['requests']=count;save()
    try:
        from app.service.skill_service import SkillService
        from app.param.skill_param import SkillContent,SkillRevision
        from types import SimpleNamespace
        user=SimpleNamespace(id=uuid4(),tenant_id=uuid4())
        service=SkillService()
        async with storage.session() as session:
            async with tenant_transaction(session,TenantContext(user.tenant_id,user.id)):
                await session.execute(text('INSERT INTO tenants VALUES(:id)'),dict(id=user.tenant_id))
                await session.execute(text('INSERT INTO users VALUES(:id,:tenant)'),dict(id=user.id,tenant=user.tenant_id))
                guide=await service.create(session,user,SkillContent(**service.template()))
                await service.change(session,user,guide['id'],SkillRevision(expected_revision=1),'published')
        meta['guide_sha256']=hashlib.sha256(service.template()['body'].encode()).hexdigest()
        profile=with_skills(LEGACY_POLICY)
        policies=PolicyProvider((profile,),profile.version)
        meta['policy_version']=profile.version; meta['status']='running';save()
        schedule=[];rng=random.Random(20261025)
        cases=dataset['cases'][:args.case_limit] if args.case_limit else dataset['cases']
        for repeat in range(1,args.repeats+1):
            for case in cases:
                arms=['A','B','C'];rng.shuffle(arms)
                schedule.extend((case,repeat,arm) for arm in arms)
        (output/'schedule.json').write_text(json.dumps([(c['id'],r,a) for c,r,a in schedule]),encoding='utf-8')
        async with httpx.AsyncClient(timeout=settings.model_request_timeout_seconds,event_hooks={'request':[limit]}) as http:
            raw=OpenAICompatibleClient(settings,http)
            consecutive_infra=0
            for case,repeat,arm in schedule:
                if count>=args.request_limit: break
                run=ClaimedRun(uuid4(),user.tenant_id,user.id,None,case.get('purpose','mail_reply_draft'))
                async with storage.session() as session:
                    async with session.begin():
                        await session.execute(text("INSERT INTO agent_runs VALUES(:id,:tenant,:user,'running')"),dict(id=run.id,tenant=user.tenant_id,user=user.id))
                harness=HarnessExecutor(storage,policy_provider=policies)
                skill=None
                if arm!='A':
                    skill=(FixedGuide if arm=='B' else SkillContextService)(SkillRepository(storage))
                    registry=ToolRegistry()
                    registry.register(ToolDefinition('skills.read','1.0','read','internal','Skill',argument_schema=SkillReadRequest),skill)
                    skill.gateway=ToolGateway(registry,harness=harness,policy_provider=policies)
                agent=MailAgent(GuardedLLMClient(raw,harness,skill_context=skill))
                row=dict(case_id=case['id'],repeat=repeat,arm=arm,run_id=str(run.id),expected_skill=case['expected_skill'])
                started=monotonic()
                try:
                    async with harness.scope(run):
                        if run.run_type=='mail_draft_revision':
                            row['output']=await agent.revise_body(case['body'],case['instruction'])
                        else:
                            row['output']=await agent.generate_reply_draft(dict(sender_name='测试客户',sender_email='client@example.com',
                                subject=case.get('subject','测试邮件'),received_at='2026-10-25T08:00:00+08:00',body_text=case['body']),
                                key_points=[],instruction=case['instruction'])
                    row['status']='generated';consecutive_infra=0
                except Exception as error:
                    row['status']='failed';row['error_code']=getattr(error,'code',type(error).__name__)
                    consecutive_infra=consecutive_infra+1 if row['error_code']=='LLM_UNAVAILABLE' else 0
                row['duration_seconds']=monotonic()-started
                async with storage.session() as session:
                    row['attempts']=[dict(x) for x in (await session.execute(text('SELECT id,kind,purpose,status,error_code,prompt_tokens,completion_tokens,total_tokens FROM run_execution_attempts WHERE run_id=:run ORDER BY started_at'),dict(run=run.id))).mappings()]
                    row['bindings']=[dict(x) for x in (await session.execute(text('SELECT status,reason,version,reads FROM run_skill_bindings WHERE run_id=:run'),dict(run=run.id))).mappings()]
                with (output/'results.jsonl').open('a',encoding='utf-8') as file: file.write(json.dumps(row,ensure_ascii=False,default=str)+'\n')
                print(case['id'],repeat,arm,row['status'],count,flush=True)
                if consecutive_infra>=3: meta['stop_reason']='three_consecutive_network_failures';break
        meta['status']='calls_finished_pending_review';save()
    finally:
        meta['requests']=count
        await storage.cleanup()
        meta['schema_cleaned']=True;meta['finished_at']=datetime.now(timezone.utc).isoformat();save()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset',type=Path,default=ROOT/'tests/evaluation/data/skill_mail_e09_v1.json')
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--repeats',type=int,choices=[1,3],default=3)
    parser.add_argument('--case-limit',type=int,default=0)
    parser.add_argument('--request-limit',type=int,required=True)
    args=parser.parse_args()
    if not 1<=args.request_limit<=630: parser.error('request limit must be 1..630')
    asyncio.run(main(args))
