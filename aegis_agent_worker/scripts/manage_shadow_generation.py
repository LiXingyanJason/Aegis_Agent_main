"""Development-only operator CLI. Shadow metadata only; no production publication."""
import argparse
import asyncio
import json
from types import SimpleNamespace
from uuid import UUID
from sqlalchemy import text
from aegis_agent_worker.config.settings import get_settings
from aegis_agent_worker.config.database import Database,TenantContext,tenant_transaction
from aegis_agent_worker.service.knowledge.shadow_generation_service import ShadowGenerationService
from aegis_agent_worker.storage.local_document_storage import LocalDocumentStorage


def arguments():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=['prepare','validate','select','rollback','show'])
    for key in ('tenant','user','document','version'): p.add_argument('--'+key,type=UUID,required=True)
    p.add_argument('--generation',type=UUID)
    p.add_argument('--request-id')
    p.add_argument('--manifest-hash')
    p.add_argument('--validation-reference')
    p.add_argument('--revision',type=int)
    p.add_argument('--title-enhancement',action='store_true')
    p.add_argument('--allow-development-shadow-write',action='store_true')
    return p.parse_args()


async def execute(args):
    settings=get_settings()
    if settings.app_env!='development': raise ValueError('Shadow CLI permits development environment only')
    if args.action!='show' and not args.allow_development_shadow_write:
        raise ValueError('Explicit development shadow write opt-in required')
    if args.action in {'validate','select','show'} and args.generation is None:
        raise ValueError('generation required')
    if args.action in {'select','rollback'} and args.revision is None:
        raise ValueError('expected revision required')
    user=SimpleNamespace(id=args.user,tenant_id=args.tenant)
    database=Database(settings);svc=ShadowGenerationService()
    try:
        async with database.session() as session:
            async with tenant_transaction(session,TenantContext(args.tenant,args.user)):
                if args.action=='prepare':
                    await svc.repo.owned(session,user,args.document,args.version)
                    source=(await session.execute(text('''SELECT source_text,storage_backend,storage_key
                      FROM knowledge_document_versions WHERE id=:version AND document_id=:doc AND tenant_id=:tenant'''),
                      dict(version=args.version,doc=args.document,tenant=args.tenant))).mappings().first()
                    if source['source_text'] is not None:
                        raw=source['source_text'].encode('utf-8')
                    elif source['storage_backend']=='local':
                        raw=await LocalDocumentStorage(settings.knowledge_storage_root).read(source['storage_key'])
                    else: raise ValueError('unsupported source backend')
                    result=await svc.prepare(session,user,args.document,args.version,args.request_id,raw,
                        dict(title_enhancement=args.title_enhancement))
                elif args.action=='validate':
                    await svc.validate(session,user,args.document,args.version,args.generation,
                        args.manifest_hash,args.validation_reference)
                    result=dict(generation_id=args.generation,status='validated',production_published=False)
                elif args.action in {'select','rollback'}:
                    result=await svc.select(session,user,args.document,args.version,args.generation,args.revision,
                        rollback=args.action=='rollback')
                else:
                    snapshot=await svc.repo.snapshot(session,user,args.document,args.version,args.generation)
                    result={k:snapshot[k] for k in ('manifest_hash','status')}
        print(json.dumps(dict(**result,mode='shadow_only'),ensure_ascii=False,default=str))
    finally:
        await database.dispose()


if __name__=='__main__':
    asyncio.run(execute(arguments()))
