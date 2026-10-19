"""手动记忆生命周期写入；事务由API管理，串行化同用户的实体确认。"""
import json
from uuid import UUID
from sqlalchemy import text


class MemoryWriteError(ValueError):
    def __init__(self,code,message,status=409,**metadata):
        self.code,self.message,self.status,self.metadata=code,message,status,metadata
        super().__init__(code)


class MemoryLifecycleRepository:
    async def lock(self,session,user):
        await session.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(CAST(:key AS text),0))'),
                              {'key':f'memory:{user.tenant_id}:{user.id}'})

    async def source(self,session,user,param):
        kind,obj,version=param.source_kind,param.source_object_id,param.source_version
        if kind=='manual': return kind,None,None
        if kind=='knowledge_document':
            try: version=str(UUID(version))
            except (ValueError,TypeError): raise MemoryWriteError('MEMORY_SOURCE_INVALID','资料版本必须是有效UUID',422) from None
        else:
            result=await session.execute(text("""SELECT md5(id::text||':'||status||':'||extract(epoch FROM updated_at)::text) AS version
                FROM agent_runs WHERE id=:id AND tenant_id=:tenant AND user_id=:user"""),
                dict(id=obj,tenant=user.tenant_id,user=user.id))
            row=result.mappings().first()
            if row is None or (version is not None and row['version']!=version):
                raise MemoryWriteError('MEMORY_SOURCE_UNAVAILABLE','来源不存在、版本已变或无权访问',404)
            version=row['version']
        valid=await session.execute(text('SELECT aegis_memory_source_accessible(:tenant,:user,:kind,:object,:version) AS valid'),
                                   dict(tenant=user.tenant_id,user=user.id,kind=kind,object=obj,version=version))
        if not valid.mappings().one()['valid']:
            raise MemoryWriteError('MEMORY_SOURCE_UNAVAILABLE','来源不存在、版本已变或无权访问',404)
        return kind,obj,version

    async def save_draft(self,session,user,param,*,memory_id=None):
        await self.lock(session,user)
        kind,obj,source_version=await self.source(session,user,param)
        values=dict(tenant=user.tenant_id,user=user.id,content=param.content,
            sensitive=param.is_sensitive,sensitivity=param.sensitivity,type=param.memory_type,
            entity_key=param.entity.key if param.entity else None,
            entity_value=json.dumps(param.entity.model_dump(),ensure_ascii=False) if param.entity else None,
            expires=param.expires_at,kind=kind,object=obj,source_version=source_version,
            source_run=obj if kind=='run' else None)
        assignments="""content=:content,is_sensitive=:sensitive,sensitivity=:sensitivity,memory_type=:type,
            entity_key=:entity_key,entity_value=CAST(:entity_value AS jsonb),expires_at=:expires,
            source_kind=:kind,source_object_id=:object,source_version=:source_version,source_run_id=:source_run,
            source='user_input',status='pending',confirmed_at=NULL,version=version+1,updated_at=now()"""
        if memory_id is None:
            result=await session.execute(text("""INSERT INTO memory_items(tenant_id,user_id,content,is_sensitive,sensitivity,
                memory_type,entity_key,entity_value,expires_at,source_kind,source_object_id,source_version,source_run_id,source)
                VALUES(:tenant,:user,:content,:sensitive,:sensitivity,:type,:entity_key,CAST(:entity_value AS jsonb),:expires,
                :kind,:object,:source_version,:source_run,'user_input') RETURNING id"""),values)
        else:
            result=await session.execute(text('UPDATE memory_items SET '+assignments+""" WHERE id=:id AND tenant_id=:tenant
                AND user_id=:user AND deleted_at IS NULL AND status IN ('pending','confirmed','revoked')
                AND version=:expected RETURNING id"""),{**values,'id':memory_id,'expected':param.expected_version})
        row=result.mappings().first()
        if row is None: raise MemoryWriteError('MEMORY_VERSION_CONFLICT','记忆不存在、已覆盖或版本已变，请刷新后重试')
        await session.execute(text("UPDATE memory_deletion_tokens SET status='revoked' WHERE memory_id=:id AND requested_by=:user AND tenant_id=:tenant AND status='active'"),
                              dict(id=row['id'],user=user.id,tenant=user.tenant_id))
        return row['id']

    async def confirm(self,session,user,memory_id,expected_version,replace_id=None,replace_version=None):
        await self.lock(session,user)
        result=await session.execute(text('SELECT * FROM memory_items WHERE id=:id AND tenant_id=:tenant AND user_id=:user AND deleted_at IS NULL FOR UPDATE'),
                                     dict(id=memory_id,tenant=user.tenant_id,user=user.id))
        row=result.mappings().first()
        if row is None: raise MemoryWriteError('MEMORY_NOT_FOUND','记忆不存在或无权访问',404)
        if row['version']!=expected_version: raise MemoryWriteError('MEMORY_VERSION_CONFLICT','记忆版本已变，请刷新核对')
        if row['status'] not in ('pending','confirmed'): raise MemoryWriteError('MEMORY_STATE_CONFLICT','该状态不能确认，请新建或编辑草稿')
        valid=await session.execute(text("""SELECT (CAST(:expires AS timestamptz) IS NULL OR CAST(:expires AS timestamptz)>now()) AND
            aegis_memory_source_accessible(:tenant,:user,:kind,:object,:version) AS valid"""),
            dict(expires=row['expires_at'],tenant=user.tenant_id,user=user.id,kind=row['source_kind'],
                 object=row['source_object_id'],version=row['source_version']))
        if not valid.mappings().one()['valid']: raise MemoryWriteError('MEMORY_SOURCE_OR_EXPIRY_INVALID','记忆已过期或来源不可访问，请重新核对')
        current=None
        if row['entity_key'] is not None:
            result=await session.execute(text("""SELECT id,version FROM memory_items WHERE tenant_id=:tenant AND user_id=:user
                AND scope='user' AND entity_key=:key AND status='confirmed' AND deleted_at IS NULL AND id<>:id FOR UPDATE"""),
                dict(tenant=user.tenant_id,user=user.id,key=row['entity_key'],id=memory_id))
            current=result.mappings().first()
        if current and (current['id']!=replace_id or current['version']!=replace_version):
            raise MemoryWriteError('MEMORY_ENTITY_CONFLICT','已有同一实体的确认值，请明确核对并选择替换',
                                   conflicting_memory_id=str(current['id']),conflicting_version=current['version'])
        if not current and replace_id is not None:
            raise MemoryWriteError('MEMORY_REPLACEMENT_STALE','待替换的记忆已变化，请刷新后重新选择')
        if current:
            await session.execute(text("UPDATE memory_items SET status='superseded',version=version+1,updated_at=now() WHERE id=:id AND tenant_id=:tenant AND user_id=:user"),
                                  dict(id=current['id'],tenant=user.tenant_id,user=user.id))
            await self._revoke_tokens(session,user,current['id'])
        if row['status']=='pending':
            await session.execute(text("""UPDATE memory_items SET status='confirmed',source='user_confirmed',confirmed_at=now(),
                supersedes_id=:old,version=version+1,updated_at=now() WHERE id=:id AND tenant_id=:tenant AND user_id=:user"""),
                dict(id=memory_id,old=current['id'] if current else None,tenant=user.tenant_id,user=user.id))
            await self._revoke_tokens(session,user,memory_id)
        return memory_id

    async def _revoke_tokens(self,session,user,identifier):
        await session.execute(text("UPDATE memory_deletion_tokens SET status='revoked' WHERE memory_id=:id AND tenant_id=:tenant AND requested_by=:user AND status='active'"),
                              dict(id=identifier,tenant=user.tenant_id,user=user.id))

    async def revoke(self,session,user,memory_id,expected_version):
        """版本CAS取消确认，不删正文；恢复须重新编辑/核对，不自动确认。"""
        await self.lock(session,user)
        row=(await session.execute(text('SELECT version,status FROM memory_items WHERE id=:id AND tenant_id=:tenant AND user_id=:user AND deleted_at IS NULL FOR UPDATE'),
            dict(id=memory_id,tenant=user.tenant_id,user=user.id))).mappings().first()
        if row is None: raise MemoryWriteError('MEMORY_NOT_FOUND','记忆不存在或无权访问',404)
        if row['version']!=expected_version: raise MemoryWriteError('MEMORY_VERSION_CONFLICT','版本已变化，请刷新后核对')
        if row['status']!='confirmed': raise MemoryWriteError('MEMORY_STATE_CONFLICT','只有已确认的记忆可以取消确认')
        await session.execute(text("UPDATE memory_items SET status='revoked',version=version+1,updated_at=now() WHERE id=:id AND tenant_id=:tenant AND user_id=:user"),
                              dict(id=memory_id,tenant=user.tenant_id,user=user.id))
        await self._revoke_tokens(session,user,memory_id)
        return memory_id
