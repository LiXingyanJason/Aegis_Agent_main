"""017真实PG测试：临时schema/受限角色全部回滚，无业务库迁移。"""
from datetime import datetime,timezone,timedelta
import importlib.util
import os
from pathlib import Path
import re
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest

from app.param.memory_create_param import MemoryCreateParam,MemoryUpdateParam
from app.repository.memory_repository import MemoryRepository
from app.repository.memory_lifecycle_repository import MemoryLifecycleRepository,MemoryWriteError

pytestmark=pytest.mark.skipif(os.getenv('AEGIS_MEMORY_DB_TESTS')!='1',reason='显式启用可回滚记忆迁移测试')


@pytest.fixture
def database():
    import psycopg
    from psycopg import sql
    from psycopg.rows import dict_row
    from app.config.settings import get_settings
    conn=psycopg.connect(get_settings().database_async_url.replace('postgresql+asyncpg://','postgresql://'),connect_timeout=5,row_factory=dict_row)
    schema,role='aegis_memory_test_'+uuid4().hex,'aegis_memory_role_'+uuid4().hex
    user=SimpleNamespace(id=uuid4(),tenant_id=uuid4())
    other=SimpleNamespace(id=uuid4(),tenant_id=uuid4())
    try:
        conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
        conn.execute(sql.SQL('SET LOCAL search_path TO {},public').format(sql.Identifier(schema)))
        conn.execute("SET LOCAL statement_timeout='5s'")
        conn.execute('CREATE TABLE tenants(id uuid PRIMARY KEY)')
        conn.execute('CREATE TABLE users(id uuid PRIMARY KEY,tenant_id uuid)')
        conn.execute('CREATE TABLE conversations(id uuid PRIMARY KEY,tenant_id uuid,user_id uuid,status text)')
        conn.execute('CREATE TABLE agent_runs(id uuid PRIMARY KEY,tenant_id uuid,user_id uuid,status text,updated_at timestamptz,conversation_id uuid)')
        conn.execute('CREATE TABLE knowledge_documents(id uuid PRIMARY KEY,tenant_id uuid,user_id uuid,visibility text,deleted_at timestamptz,enabled boolean,is_sensitive boolean,external_embedding_allowed boolean,current_version_id uuid)')
        conn.execute('CREATE TABLE knowledge_document_versions(id uuid PRIMARY KEY,tenant_id uuid,document_id uuid,status text)')
        conn.execute("""CREATE TABLE memory_items(id uuid PRIMARY KEY DEFAULT gen_random_uuid(),tenant_id uuid,user_id uuid,
          content text,source text,is_sensitive boolean DEFAULT false,source_run_id uuid,created_at timestamptz DEFAULT now(),
          updated_at timestamptz DEFAULT now(),deleted_at timestamptz,deleted_by uuid)""")
        conn.execute("""CREATE TABLE memory_deletion_tokens(id uuid PRIMARY KEY DEFAULT gen_random_uuid(),tenant_id uuid,memory_id uuid,
          requested_by uuid,token_hash text,status text,expires_at timestamptz,used_at timestamptz)""")
        for setting,name in (('app.tenant_id','aegis_current_tenant_id'),('app.user_id','aegis_current_user_id')):
            conn.execute(f"CREATE FUNCTION {name}() RETURNS uuid LANGUAGE sql STABLE AS $$ SELECT NULLIF(current_setting('{setting}',true),'')::uuid $$")
        for actor in (user,other):
            conn.execute('INSERT INTO tenants VALUES (%s)',(actor.tenant_id,))
            conn.execute('INSERT INTO users VALUES (%s,%s)',(actor.id,actor.tenant_id))
        legacy=uuid4()
        conn.execute("INSERT INTO memory_items(id,tenant_id,user_id,content,source,is_sensitive) VALUES(%s,%s,%s,'原有偏好','user_input',false)",(legacy,user.tenant_id,user.id))
        conn.execute("INSERT INTO memory_items(tenant_id,user_id,content,source,is_sensitive) VALUES(%s,%s,'原有敏感偏好','user_input',true)",(user.tenant_id,user.id))
        path=Path(__file__).resolve().parents[2]/'migrations/versions/017_memory_manual_lifecycle.py'
        spec=importlib.util.spec_from_file_location('memory_migration',path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with patch.object(module,'op',SimpleNamespace(execute=conn.execute)): module.upgrade()
        conn.execute('ALTER TABLE memory_items ENABLE ROW LEVEL SECURITY')
        conn.execute('CREATE POLICY memory_owner ON memory_items USING(tenant_id=aegis_current_tenant_id() AND user_id=aegis_current_user_id()) WITH CHECK(tenant_id=aegis_current_tenant_id() AND user_id=aegis_current_user_id())')
        conn.execute(sql.SQL('CREATE ROLE {} NOLOGIN NOSUPERUSER NOBYPASSRLS').format(sql.Identifier(role)))
        conn.execute(sql.SQL('GRANT USAGE ON SCHEMA {} TO {}').format(sql.Identifier(schema),sql.Identifier(role)))
        conn.execute(sql.SQL('GRANT SELECT,INSERT,UPDATE ON ALL TABLES IN SCHEMA {} TO {}').format(sql.Identifier(schema),sql.Identifier(role)))
        conn.execute(sql.SQL('SET LOCAL ROLE {}').format(sql.Identifier(role)))
        conn.execute("SELECT set_config('app.tenant_id',%s,true),set_config('app.user_id',%s,true)",(str(user.tenant_id),str(user.id)))

        async def execute(statement,params=None):
            query=re.sub(r'(?<!:):(\w+)',lambda m:'%('+m[1]+')s',str(statement))
            cursor=conn.execute(query,params or {})
            rows=cursor.fetchall() if cursor.description else []
            def scalar_one():
                from sqlalchemy.exc import NoResultFound
                if not rows:
                    raise NoResultFound()
                return next(iter(rows[0].values()))
            return SimpleNamespace(mappings=lambda:SimpleNamespace(first=lambda:rows[0] if rows else None,
                one=lambda:rows[0],all=lambda:rows),rowcount=cursor.rowcount,
                scalar_one=scalar_one)
        yield conn,SimpleNamespace(execute=execute),user,other,legacy,module,role
    finally:
        conn.rollback();conn.close()


async def draft(session,user,**changes):
    return await MemoryLifecycleRepository().save_draft(session,user,MemoryCreateParam.model_validate({'content':'用户说明',**changes}))


@pytest.mark.asyncio
async def test_optional_entity_description_is_persisted_and_regenerated_on_edit(database):
    conn,session,user,_,_,_,_=database
    writes=MemoryLifecycleRepository()
    identifier=await writes.save_draft(session,user,MemoryCreateParam(memory_type='entity',
        entity={'key':'timezone','value':'Asia/Shanghai'}))
    initial=conn.execute('SELECT content,status FROM memory_items WHERE id=%s',(identifier,)).fetchone()
    assert initial['content']=='默认时区：Asia/Shanghai' and initial['status']=='pending'
    await writes.confirm(session,user,identifier,1)
    await writes.save_draft(session,user,MemoryUpdateParam(memory_type='entity',sensitivity='normal',
        source_kind='manual',expires_at=None,expected_version=2,entity={'key':'timezone','value':'Asia/Tokyo'}),memory_id=identifier)
    changed=conn.execute('SELECT content,status,version FROM memory_items WHERE id=%s',(identifier,)).fetchone()
    assert changed['content']=='默认时区：Asia/Tokyo' and changed['status']=='pending' and changed['version']==3


def worker_reader():
    path=Path(__file__).resolve().parents[3]/'aegis_agent_worker/src/aegis_agent_worker/repository/memory_repository.py'
    spec=importlib.util.spec_from_file_location('worker_memory_reads',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module.MemoryRepository()


@pytest.mark.asyncio
async def test_020_recall_before_limit_revoke_and_safe_usage_metadata(database):
    """新迁移、生产检索SQL及取消确认仅在随机schema内执行，fixture统一回滚。"""
    from psycopg import sql
    from psycopg.errors import CheckViolation
    from app.repository.memory_usage_repository import MemoryUsageRepository
    conn,session,user,other,legacy,_,role=database
    conn.execute('RESET ROLE')
    conn.execute('''CREATE TABLE run_execution_attempts(id uuid PRIMARY KEY,run_id uuid,tenant_id uuid,user_id uuid,
        kind text,purpose text,status text,started_at timestamptz DEFAULT now(),
        prompt_tokens int,completion_tokens int,total_tokens int)''')
    conn.execute('''CREATE TABLE run_memory_usages(id uuid PRIMARY KEY DEFAULT gen_random_uuid(),tenant_id uuid,
        run_id uuid,memory_id uuid REFERENCES memory_items(id),usage_purpose varchar(32),included_in_prompt boolean,
        exclusion_reason varchar(256),display_summary varchar(1000),created_at timestamptz DEFAULT now())''')
    run,attempt=uuid4(),uuid4()
    conn.execute("INSERT INTO agent_runs VALUES(%s,%s,%s,'completed',now(),NULL)",(run,user.tenant_id,user.id))
    conn.execute("INSERT INTO run_execution_attempts(id,run_id,tenant_id,user_id,kind,purpose,status) VALUES(%s,%s,%s,%s,'model','conversation_reply','completed')",
                 (attempt,run,user.tenant_id,user.id))
    conn.execute("INSERT INTO run_memory_usages(tenant_id,run_id,memory_id,usage_purpose,included_in_prompt,display_summary) VALUES(%s,%s,%s,'context',true,'旧正文副本')",
                 (user.tenant_id,run,legacy))
    for name in ('018_memory_call_usage','019_calendar_memory_preferences','020_memory_selection_and_revocation'):
        path=Path(__file__).resolve().parents[2]/f'migrations/versions/{name}.py'
        spec=importlib.util.spec_from_file_location(name,path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with patch.object(module,'op',SimpleNamespace(execute=conn.execute)): module.upgrade()
    # 020降级只撤销结构，不复活已清理审计正文；可再升级。
    adapter=SimpleNamespace(execute=conn.execute,drop_column=lambda table,column:
        conn.execute(sql.SQL('ALTER TABLE {} DROP COLUMN {}').format(sql.Identifier(table),sql.Identifier(column))))
    with patch.object(module,'op',adapter):
        module.downgrade()
        module.upgrade()
    assert conn.execute('SELECT display_summary FROM run_memory_usages').fetchone()['display_summary'] is None
    with pytest.raises(CheckViolation),conn.transaction():
        conn.execute("UPDATE run_memory_usages SET display_summary='不得再存正文'")
    with pytest.raises(CheckViolation),conn.transaction():
        conn.execute("UPDATE run_execution_attempts SET memory_selection_details='[]'::jsonb")
    schema=conn.execute('SELECT current_schema() AS name').fetchone()['name']
    conn.execute(sql.SQL('GRANT SELECT,INSERT,UPDATE ON ALL TABLES IN SCHEMA {} TO {}').format(sql.Identifier(schema),sql.Identifier(role)))
    conn.execute(sql.SQL('SET LOCAL ROLE {}').format(sql.Identifier(role)))
    # 超过原先50条最近记录：较旧但相关性更强的记录必须仍可召回。
    for index in range(55):
        conn.execute("INSERT INTO memory_items(tenant_id,user_id,content,source,status,confirmed_at) VALUES(%s,%s,%s,'user_confirmed','confirmed',now())",
                     (user.tenant_id,user.id,f'邮件会议安排{index}'))
    old=conn.execute("INSERT INTO memory_items(tenant_id,user_id,content,source,status,confirmed_at,created_at,updated_at) VALUES(%s,%s,'邮件回复语气正式','user_confirmed','confirmed',now(),'2000-01-01','2000-01-01') RETURNING id",
                     (user.tenant_id,user.id)).fetchone()['id']
    pending=await draft(session,user,content='邮件回复语气待确认')
    sensitive=await draft(session,user,content='邮件回复语气敏感',sensitivity='sensitive')
    await MemoryLifecycleRepository().confirm(session,user,sensitive,1)
    reader=worker_reader()
    hints=dict(types=['long_term_summary','entity'],terms=['邮件','回复','语气'],affinity=[],entity_keys=[],task='邮件回复语气')
    policy=SimpleNamespace(recall_timeout_seconds=2,strong_term_matches=2,candidate_limit=50)
    rows=await reader.recall_candidates(session,tenant_id=user.tenant_id,user_id=user.id,hints=hints,policy=policy)
    assert len(rows)==51 and rows[0]['id']==old and not {pending,sensitive}&{row['id'] for row in rows}
    owner=SimpleNamespace(id=run,tenant_id=user.tenant_id,user_id=user.id)
    bundle=SimpleNamespace(items=[SimpleNamespace(memory_id=old,version=1,memory_type='long_term_summary',excerpt='只用于调用',
        source=SimpleNamespace(kind='manual',object_id=old,version='1'),selection_reason='confirmed_summary')],
        purpose='conversation_reply',policy_version='memory-contract-v1')
    await reader.record_bundle_usage(session,run=owner,attempt_id=attempt,bundle=bundle)
    usage_repo=MemoryUsageRepository()
    usages=await usage_repo.usages(session,user,run,[attempt],50)
    assert usages[0]['current_status']=='available' and 'content' not in usages[0] and 'display_summary' not in usages[0]
    assert (await usage_repo.attempts(session,user,run,50))[0]['total_tokens'] is None
    token,_=await MemoryRepository().create_deletion_request(session,tenant_id=user.tenant_id,user_id=user.id,memory_id=old)
    await MemoryLifecycleRepository().revoke(session,user,old,1)
    changed=conn.execute('SELECT status,version FROM memory_items WHERE id=%s',(old,)).fetchone()
    assert changed==dict(status='revoked',version=2)
    assert conn.execute('SELECT status FROM memory_deletion_tokens WHERE memory_id=%s',(old,)).fetchone()['status']=='revoked'
    assert old not in {row['id'] for row in await reader.recall_candidates(session,tenant_id=user.tenant_id,user_id=user.id,hints=hints,policy=policy)}
    assert (await usage_repo.usages(session,user,run,[attempt],50))[0]['current_status']=='inactive'
    with pytest.raises(MemoryWriteError): await MemoryLifecycleRepository().revoke(session,user,old,1)
    assert await MemoryRepository().delete_memory(session,tenant_id=user.tenant_id,user_id=user.id,memory_id=old,deletion_token=token)=='invalid_token'
    conn.execute("SELECT set_config('app.tenant_id',%s,true),set_config('app.user_id',%s,true)",(str(other.tenant_id),str(other.id)))
    assert await usage_repo.usages(session,other,run,[attempt],50)==[]
    assert await usage_repo.attempts(session,other,run,50)==[]
    with pytest.raises(MemoryWriteError) as denied: await MemoryLifecycleRepository().revoke(session,other,old,2)
    assert denied.value.code=='MEMORY_NOT_FOUND'
    conn.execute("SELECT set_config('app.tenant_id',%s,true),set_config('app.user_id',%s,true)",(str(user.tenant_id),str(user.id)))
    with pytest.raises(MemoryWriteError): await MemoryLifecycleRepository().confirm(session,user,old,2)
    # 撤销后不得直接确认；编辑成新pending版本，核对确认后才能再次生效。
    await MemoryLifecycleRepository().save_draft(session,user,MemoryUpdateParam(content='邮件回复语气正式',expected_version=2,
        memory_type='long_term_summary',sensitivity='normal',source_kind='manual',expires_at=None),memory_id=old)
    await MemoryLifecycleRepository().confirm(session,user,old,3)
    assert (await usage_repo.usages(session,user,run,[attempt],50))[0]['current_status']=='changed'


@pytest.mark.asyncio
async def test_018_attempt_usage_metadata_tokens_and_owner_constraint(database):
    """018及Worker真实SQL仅作用于本fixture随机schema，结束统一回滚。"""
    from psycopg import sql
    from psycopg.errors import ForeignKeyViolation,CheckViolation
    conn,session,user,other,legacy,_,role=database
    conn.execute('RESET ROLE')
    conn.execute('CREATE TABLE run_execution_attempts(id uuid PRIMARY KEY,run_id uuid,tenant_id uuid,user_id uuid,kind text)')
    conn.execute("""CREATE TABLE run_memory_usages(id uuid PRIMARY KEY DEFAULT gen_random_uuid(),tenant_id uuid,
        run_id uuid,memory_id uuid REFERENCES memory_items(id),usage_purpose varchar(32),included_in_prompt boolean,
        exclusion_reason varchar(256),display_summary varchar(1000),created_at timestamptz DEFAULT now())""")
    path=Path(__file__).resolve().parents[2]/'migrations/versions/018_memory_call_usage.py'
    spec=importlib.util.spec_from_file_location('memory_call_migration',path)
    migration=importlib.util.module_from_spec(spec);spec.loader.exec_module(migration)
    with patch.object(migration,'op',SimpleNamespace(execute=conn.execute)): migration.upgrade()
    run,attempt=uuid4(),uuid4()
    conn.execute('INSERT INTO run_execution_attempts VALUES(%s,%s,%s,%s,\'model\',NULL,NULL,NULL)',
                 (attempt,run,user.tenant_id,user.id))
    # 旧记录保留未知关联与未知Token，不捏造回填。
    conn.execute("INSERT INTO run_memory_usages(tenant_id,run_id,memory_id,usage_purpose,included_in_prompt,display_summary) VALUES(%s,%s,%s,'context',true,'旧摘要')",
                 (user.tenant_id,run,legacy))
    schema=conn.execute('SELECT current_schema() AS name').fetchone()['name']
    conn.execute(sql.SQL('GRANT SELECT,INSERT,UPDATE ON ALL TABLES IN SCHEMA {} TO {}').format(sql.Identifier(schema),sql.Identifier(role)))
    conn.execute(sql.SQL('SET LOCAL ROLE {}').format(sql.Identifier(role)))
    reader=worker_reader()
    item=SimpleNamespace(memory_id=legacy,version=1,memory_type='long_term_summary',excerpt='机密正文不得审计',
        source=SimpleNamespace(kind='manual',object_id=legacy,version='1'),selection_reason='confirmed_summary')
    bundle=SimpleNamespace(items=[item],purpose='mail_reply_draft',policy_version='memory-contract-v1')
    owner=SimpleNamespace(id=run,tenant_id=user.tenant_id,user_id=user.id)
    await reader.record_bundle_usage(session,run=owner,attempt_id=attempt,bundle=bundle)
    current=conn.execute('SELECT * FROM run_memory_usages WHERE attempt_id=%s',(attempt,)).fetchone()
    assert current['display_summary'] is None and current['memory_version']==1
    assert current['model_purpose']=='mail_reply_draft' and current['estimated_tokens'] is None
    await reader.finish_bundle_usage(session,run=owner,attempt_id=attempt,
        usage=SimpleNamespace(prompt_tokens=101,completion_tokens=13,total_tokens=114))
    assert conn.execute('SELECT call_total_tokens FROM run_memory_usages WHERE attempt_id=%s',(attempt,)).fetchone()['call_total_tokens']==114
    historical=conn.execute('SELECT * FROM run_memory_usages WHERE attempt_id IS NULL').fetchone()
    assert historical['display_summary']=='旧摘要' and historical['call_prompt_tokens'] is None
    # 使用不同run/tenant伪造attempt关联，数据库也不能接受。
    for field,value in (('run_id',uuid4()),('tenant_id',other.tenant_id)):
        with pytest.raises(ForeignKeyViolation),conn.transaction():
            conn.execute(f'UPDATE run_memory_usages SET {field}=%s WHERE attempt_id=%s',(value,attempt))
    with pytest.raises(CheckViolation),conn.transaction():
        conn.execute("UPDATE run_memory_usages SET model_purpose='main_routing' WHERE attempt_id=%s",(attempt,))
    # 选择后发生修改退回pending，在真正提交使用审计时再次阻断旧版本。
    from sqlalchemy.exc import NoResultFound
    next_attempt=uuid4()
    conn.execute("INSERT INTO run_execution_attempts VALUES(%s,%s,%s,%s,'model',NULL,NULL,NULL)",
                 (next_attempt,run,user.tenant_id,user.id))
    conn.execute("UPDATE memory_items SET status='pending',confirmed_at=NULL,version=version+1 WHERE id=%s",(legacy,))
    with pytest.raises(NoResultFound):
        await reader.record_bundle_usage(session,run=owner,attempt_id=next_attempt,bundle=bundle)
    assert conn.execute('SELECT count(*) AS n FROM run_memory_usages WHERE attempt_id=%s',(next_attempt,)).fetchone()['n']==0


@pytest.mark.asyncio
async def test_019_structured_meeting_entity_confirmation_and_calendar_call_audit(database):
    from psycopg import sql
    from psycopg.errors import CheckViolation,RaiseException
    conn,session,user,_,_,_,role=database
    conn.execute('RESET ROLE')
    conn.execute('CREATE TABLE run_execution_attempts(id uuid PRIMARY KEY,run_id uuid,tenant_id uuid,user_id uuid,kind text)')
    conn.execute("""CREATE TABLE run_memory_usages(id uuid PRIMARY KEY DEFAULT gen_random_uuid(),tenant_id uuid,
        run_id uuid,memory_id uuid REFERENCES memory_items(id),usage_purpose varchar(32),included_in_prompt boolean,
        exclusion_reason varchar(256),display_summary varchar(1000),created_at timestamptz DEFAULT now())""")
    modules=[]
    for name in ('018_memory_call_usage','019_calendar_memory_preferences'):
        path=Path(__file__).resolve().parents[2]/f'migrations/versions/{name}.py'
        spec=importlib.util.spec_from_file_location(name,path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with patch.object(module,'op',SimpleNamespace(execute=conn.execute)): module.upgrade()
        modules.append(module)
    # 无新格式数据时可正常降级/重升；不应依赖只走升级的偶然环境。
    with patch.object(modules[-1],'op',SimpleNamespace(execute=conn.execute)):
        modules[-1].downgrade()
        modules[-1].upgrade()
    schema=conn.execute('SELECT current_schema() AS name').fetchone()['name']
    conn.execute(sql.SQL('GRANT SELECT,INSERT,UPDATE ON ALL TABLES IN SCHEMA {} TO {}').format(sql.Identifier(schema),sql.Identifier(role)))
    conn.execute(sql.SQL('SET LOCAL ROLE {}').format(sql.Identifier(role)))
    identifier=await draft(session,user,memory_type='entity',entity={
        'key':'meeting_preferences','preferred_time_of_day':'afternoon','avoid_weekdays':[1]})
    reader=worker_reader()
    assert identifier not in {item['id'] for item in await reader.list_active_non_sensitive(session,tenant_id=user.tenant_id,user_id=user.id)}
    await MemoryLifecycleRepository().confirm(session,user,identifier,1)
    assert identifier in {item['id'] for item in await reader.list_active_non_sensitive(session,tenant_id=user.tenant_id,user_id=user.id)}
    run,attempt=uuid4(),uuid4()
    conn.execute("INSERT INTO run_execution_attempts VALUES(%s,%s,%s,%s,'model',NULL,NULL,NULL)",(attempt,run,user.tenant_id,user.id))
    owner=SimpleNamespace(id=run,tenant_id=user.tenant_id,user_id=user.id)
    bundle=SimpleNamespace(purpose='calendar_draft_personalization',policy_version='calendar-memory-v1',items=[
        SimpleNamespace(memory_id=identifier,version=2,memory_type='entity',excerpt='结构字段',
            source=SimpleNamespace(kind='manual',object_id=identifier,version='2'),selection_reason='relevant_confirmed_preference')])
    await reader.record_bundle_usage(session,run=owner,attempt_id=attempt,bundle=bundle)
    assert conn.execute('SELECT model_purpose FROM run_memory_usages WHERE attempt_id=%s',(attempt,)).fetchone()['model_purpose']=='calendar_draft_personalization'
    with pytest.raises(CheckViolation),conn.transaction():
        conn.execute("UPDATE run_memory_usages SET memory_type='long_term_summary' WHERE attempt_id=%s",(attempt,))
    # 降级不得为恢复旧约束删除记忆或调用记录。
    conn.execute('RESET ROLE')
    with pytest.raises(RaiseException),conn.transaction():
        with patch.object(modules[-1],'op',SimpleNamespace(execute=conn.execute)): modules[-1].downgrade()


@pytest.mark.asyncio
async def test_legacy_mapping_draft_confirmation_and_edit_invalidation(database):
    conn,session,user,other,legacy,_,_=database
    reads,writes=MemoryRepository(),MemoryLifecycleRepository()
    historical=await reads.find_memory(session,tenant_id=user.tenant_id,user_id=user.id,memory_id=legacy)
    assert historical['status']=='confirmed' and historical['confirmed_at'] is not None and historical['source_kind']=='manual'
    old_sensitive=conn.execute("SELECT sensitivity,is_sensitive FROM memory_items WHERE content='原有敏感偏好'").fetchone()
    assert old_sensitive['sensitivity']=='sensitive' and old_sensitive['is_sensitive']
    identifier=await draft(session,user)
    values=dict(tenant_id=user.tenant_id,user_id=user.id,memory_id=identifier)
    assert not (await reads.find_memory(session,**values))['prompt_eligible']
    await writes.confirm(session,user,identifier,1)
    assert (await reads.find_memory(session,**values))['prompt_eligible']
    await writes.save_draft(session,user,MemoryUpdateParam(content='手动修改',expected_version=2,
        memory_type='long_term_summary',sensitivity='normal',source_kind='manual',expires_at=None),memory_id=identifier)
    changed=await reads.find_memory(session,**values)
    assert changed['status']=='pending' and changed['version']==3 and changed['confirmed_at'] is None and not changed['prompt_eligible']
    assert await reads.find_memory(session,tenant_id=other.tenant_id,user_id=other.id,memory_id=identifier) is None
    assert conn.execute('SELECT count(*) AS n FROM memory_item_revisions WHERE memory_id=%s',(identifier,)).fetchone()['n']==3
    conn.execute("SELECT set_config('app.tenant_id',%s,true),set_config('app.user_id',%s,true)",(str(other.tenant_id),str(other.id)))
    assert conn.execute('SELECT count(*) AS n FROM memory_item_revisions').fetchone()['n']==0


@pytest.mark.asyncio
async def test_entity_requires_explicit_replacement_and_retains_old_row(database):
    conn,session,user,_,_,_,_=database
    writes=MemoryLifecycleRepository()
    first=await draft(session,user,memory_type='entity',entity={'key':'timezone','value':'Asia/Shanghai'})
    second=await draft(session,user,memory_type='entity',entity={'key':'timezone','value':'Europe/London'})
    await writes.confirm(session,user,first,1)
    # 唯一索引是数据库最后一道守卫，不仅依赖API内的锁与预检查。
    conn.execute('SAVEPOINT unique_entity_guard')
    try:
        import psycopg
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute("""INSERT INTO memory_items(tenant_id,user_id,content,source,memory_type,entity_key,entity_value,status,confirmed_at)
                VALUES(%s,%s,'不能双活','user_confirmed','entity','timezone','{"key":"timezone","value":"UTC"}','confirmed',now())""",(user.tenant_id,user.id))
    finally: conn.execute('ROLLBACK TO SAVEPOINT unique_entity_guard')
    with pytest.raises(MemoryWriteError) as error: await writes.confirm(session,user,second,1)
    assert error.value.code=='MEMORY_ENTITY_CONFLICT' and error.value.metadata['conflicting_version']==2
    with pytest.raises(MemoryWriteError): await writes.confirm(session,user,second,1,first,1)
    await writes.confirm(session,user,second,1,first,2)
    rows=conn.execute('SELECT id,status,version,supersedes_id FROM memory_items WHERE entity_key IS NOT NULL').fetchall()
    assert sum(row['status']=='confirmed' for row in rows)==1
    assert next(row for row in rows if row['id']==first)['status']=='superseded'
    assert next(row for row in rows if row['id']==second)['supersedes_id']==first
    with pytest.raises(MemoryWriteError): await writes.confirm(session,user,second,1)


@pytest.mark.asyncio
async def test_source_permission_version_and_expiry_filter_immediately(database):
    conn,session,user,other,_,_,_=database
    document,version=uuid4(),uuid4()
    peer=uuid4()
    conn.execute('INSERT INTO users VALUES(%s,%s)',(peer,user.tenant_id))
    conn.execute("INSERT INTO knowledge_documents VALUES(%s,%s,%s,'tenant',NULL,true,false,true,%s)",(document,user.tenant_id,peer,version))
    conn.execute("INSERT INTO knowledge_document_versions VALUES(%s,%s,%s,'ready')",(version,user.tenant_id,document))
    identifier=await draft(session,user,source_kind='knowledge_document',source_object_id=document,source_version=str(version))
    writes,reads=MemoryLifecycleRepository(),MemoryRepository()
    await writes.confirm(session,user,identifier,1)
    args=dict(tenant_id=user.tenant_id,user_id=user.id,memory_id=identifier)
    assert (await reads.find_memory(session,**args))['prompt_eligible']
    async def selected():
        return {row['id'] for row in await worker_reader().list_active_non_sensitive(session,tenant_id=user.tenant_id,user_id=user.id)}
    assert identifier in await selected()
    conn.execute('UPDATE knowledge_documents SET deleted_at=now()')
    assert not (await reads.find_memory(session,**args))['prompt_eligible']
    assert identifier not in await selected()
    conn.execute('UPDATE knowledge_documents SET deleted_at=NULL')
    conn.execute("UPDATE knowledge_documents SET visibility='private'")
    assert not (await reads.find_memory(session,**args))['prompt_eligible']
    assert identifier not in await selected()
    conn.execute("UPDATE knowledge_documents SET visibility='tenant'")
    conn.execute('UPDATE knowledge_documents SET current_version_id=%s',(uuid4(),))
    assert not (await reads.find_memory(session,**args))['prompt_eligible']
    with pytest.raises(MemoryWriteError): await draft(session,user,source_kind='knowledge_document',source_object_id=document,source_version=str(version))
    sensitive=await draft(session,user,sensitivity='sensitive')
    await writes.confirm(session,user,sensitive,1)
    assert not (await reads.find_memory(session,**{**args,'memory_id':sensitive}))['prompt_eligible']
    normal=await draft(session,user,expires_at=datetime.now(timezone.utc)+timedelta(days=1))
    await writes.confirm(session,user,normal,1)
    conn.execute("UPDATE memory_items SET expires_at=now()-interval '1 second' WHERE id=%s",(normal,))
    assert not (await reads.find_memory(session,**{**args,'memory_id':normal}))['prompt_eligible']


@pytest.mark.asyncio
async def test_real_worker_sql_excludes_pending_sensitive_expired_and_revoked_sources(database):
    conn,session,user,_,_,_,_=database
    writes=MemoryLifecycleRepository()
    pending=await draft(session,user)
    sensitive=await draft(session,user,sensitivity='sensitive');await writes.confirm(session,user,sensitive,1)
    normal=await draft(session,user);await writes.confirm(session,user,normal,1)
    expired=await draft(session,user);await writes.confirm(session,user,expired,1)
    conn.execute("UPDATE memory_items SET expires_at=now()-interval '1 second' WHERE id=%s",(expired,))
    rows=await worker_reader().list_active_non_sensitive(session,tenant_id=user.tenant_id,user_id=user.id)
    ids={row['id'] for row in rows}
    assert normal in ids and not ids & {pending,sensitive,expired}


@pytest.mark.asyncio
async def test_deleted_records_and_old_delete_token_cannot_affect_new_version(database):
    _,session,user,_,_,_,_=database
    reads,writes=MemoryRepository(),MemoryLifecycleRepository()
    identifier=await draft(session,user)
    token,_=await reads.create_deletion_request(session,tenant_id=user.tenant_id,user_id=user.id,memory_id=identifier)
    await writes.confirm(session,user,identifier,1)
    assert await reads.delete_memory(session,tenant_id=user.tenant_id,user_id=user.id,memory_id=identifier,deletion_token=token)=='invalid_token'
    token,_=await reads.create_deletion_request(session,tenant_id=user.tenant_id,user_id=user.id,memory_id=identifier)
    assert await reads.delete_memory(session,tenant_id=user.tenant_id,user_id=user.id,memory_id=identifier,deletion_token=token)=='deleted'
    assert await reads.find_memory(session,tenant_id=user.tenant_id,user_id=user.id,memory_id=identifier) is None


@pytest.mark.asyncio
async def test_real_run_source_is_not_forged_and_changes_invalidate(database):
    conn,session,user,other,_,_,_=database
    run=uuid4()
    conn.execute("INSERT INTO agent_runs VALUES(%s,%s,%s,'completed',now(),NULL)",(run,user.tenant_id,user.id))
    identifier=await draft(session,user,source_run_id=run)
    await MemoryLifecycleRepository().confirm(session,user,identifier,1)
    args=dict(tenant_id=user.tenant_id,user_id=user.id,memory_id=identifier)
    item=await MemoryRepository().find_memory(session,**args)
    assert item['source_kind']=='run' and item['source_object_id']==run and item['source_version'] and item['prompt_eligible']
    conn.execute("UPDATE agent_runs SET status='failed'")
    assert not (await MemoryRepository().find_memory(session,**args))['prompt_eligible']
    with pytest.raises(MemoryWriteError): await draft(session,user,source_run_id=uuid4())


def test_downgrade_baseline_and_reject_lifecycle_data(database):
    conn,_,_,_,_,module,_=database
    conn.execute('RESET ROLE')
    with patch.object(module,'op',SimpleNamespace(execute=conn.execute)):
        module.downgrade()
        module.upgrade()
    conn.execute("UPDATE memory_items SET status='pending',confirmed_at=NULL")
    conn.execute('SAVEPOINT downgrade_guard')
    try:
        with patch.object(module,'op',SimpleNamespace(execute=conn.execute)):
            with pytest.raises(Exception,match='Cannot downgrade'): module.downgrade()
    finally: conn.execute('ROLLBACK TO SAVEPOINT downgrade_guard')
