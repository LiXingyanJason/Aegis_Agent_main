from hashlib import sha256
from types import SimpleNamespace
from uuid import uuid4
import pytest
from aegis_agent_worker.indexing.generation import build_manifest,GenerationConfig
from aegis_agent_worker.service.knowledge.shadow_generation_service import ShadowGenerationService
from aegis_agent_worker.harness.exceptions import HarnessError


def test_manifest_determinism_bom_and_generation_isolation():
    ids=[uuid4() for _ in range(3)]
    raw=b'\xef\xbb\xbf'+('# 文档\n## 第二条\n条件。\n\n例外。').encode()
    a=build_manifest(*ids,raw,'标题',GenerationConfig())
    assert a==build_manifest(*ids,raw,'标题',GenerationConfig())
    assert a['source_hash']==sha256(raw).hexdigest()!=a['canonical_hash']
    b=build_manifest(ids[0],ids[1],uuid4(),raw,'标题',GenerationConfig())
    assert a['chunks'][0]['chunk_id']!=b['chunks'][0]['chunk_id']
    e=build_manifest(*ids,raw,'标题',GenerationConfig(title_enhancement=True))
    assert e['manifest_hash']!=a['manifest_hash']
    assert e['chunks'][0]['raw']==a['chunks'][0]['raw']


class Repo:
    def __init__(self,raw): self.raw=raw;self.row=None;self.saves=0
    async def lock_request(self,*args): pass
    async def owned(self,*args): return dict(source_hash=sha256(self.raw).hexdigest(),title='测试')
    async def existing(self,*args): return self.row
    async def save(self,session,user,doc,ver,request,request_hash,manifest):
        self.saves+=1
        self.row=dict(id=manifest['scope']['generation_id'],request_hash=request_hash,manifest_hash=manifest['manifest_hash'])


@pytest.mark.asyncio
async def test_prepare_idempotency_config_conflict_source_change():
    raw='源文。'.encode();repo=Repo(raw);svc=ShadowGenerationService(repo)
    user=SimpleNamespace(id=uuid4(),tenant_id=uuid4());doc,ver=uuid4(),uuid4()
    first=await svc.prepare(None,user,doc,ver,'request',raw)
    retry=await svc.prepare(None,user,doc,ver,'request',raw)
    assert str(first['generation_id'])==str(retry['generation_id']) and retry['reused'] and repo.saves==1
    with pytest.raises(HarnessError,match='相同重建请求'):
        await svc.prepare(None,user,doc,ver,'request',raw,dict(title_enhancement=True))
    with pytest.raises(HarnessError,match='hash'):
        await svc.prepare(None,user,doc,ver,'other',b'changed')


@pytest.mark.asyncio
async def test_invalid_revision_and_config_fail_without_publication():
    svc=ShadowGenerationService(Repo(b'x'))
    with pytest.raises(ValueError): await svc.select(None,None,None,None,None,True)
    with pytest.raises(ValueError): GenerationConfig(size=10,overlap=20).policy()
    with pytest.raises(ValueError): await svc.validate(None,None,None,None,None,'a'*64,'')
