"""共享版本索引绑定；支持所有者软删除，删除后仍禁止内容读取。"""
from alembic import op

revision='015_knowledge_version_binding'
down_revision='014_mail_workbench_flags'
branch_labels=depends_on=None


def _policies(deletion):
    owner='tenant_id=aegis_current_tenant_id() AND user_id=aegis_current_user_id()'
    readable=("tenant_id=aegis_current_tenant_id() AND (user_id=aegis_current_user_id() OR (visibility='tenant' AND deleted_at IS NULL))"
        if deletion else "tenant_id=aegis_current_tenant_id() AND (user_id=aegis_current_user_id() OR visibility='tenant') AND deleted_at IS NULL")
    # PostgreSQL UPDATE也检查SELECT新行策略。所有者仅可见已删除文档的元数据，以完成软删除；
    # API仍显式隐藏已删除文档；子表所有SELECT/ALL策略必须同时排除deleted，不能只改读策略。
    op.execute(f'ALTER POLICY knowledge_document_read ON knowledge_documents USING ({readable})')
    alive=' AND d.deleted_at IS NULL' if deletion else ''
    for table in ('knowledge_document_versions','knowledge_chunks'):
        exists=f'EXISTS(SELECT 1 FROM knowledge_documents d WHERE d.id={table}.document_id AND d.tenant_id={table}.tenant_id{alive})'
        owns=f'EXISTS(SELECT 1 FROM knowledge_documents d WHERE d.id={table}.document_id AND d.tenant_id={table}.tenant_id AND d.user_id=aegis_current_user_id(){alive})'
        op.execute(f'ALTER POLICY {table}_read ON {table} USING (tenant_id=aegis_current_tenant_id() AND {exists})')
        op.execute(f'ALTER POLICY {table}_write ON {table} USING (tenant_id=aegis_current_tenant_id() AND {owns}) WITH CHECK (tenant_id=aegis_current_tenant_id() AND {owns})')
    predicate=f'{owner} AND EXISTS(SELECT 1 FROM knowledge_documents d WHERE d.id=knowledge_index_jobs.document_id AND d.tenant_id=knowledge_index_jobs.tenant_id AND d.user_id=aegis_current_user_id(){alive})'
    op.execute(f'ALTER POLICY knowledge_jobs_owner ON knowledge_index_jobs USING ({predicate}) WITH CHECK ({predicate})')


def upgrade():
    op.execute('ALTER TABLE knowledge_document_versions ADD COLUMN vector_namespace char(64)')
    op.execute('ALTER TABLE knowledge_document_versions ADD COLUMN vector_collection varchar(128)')
    op.execute("""UPDATE knowledge_document_versions v SET vector_namespace=j.vector_namespace,vector_collection=j.vector_collection
        FROM knowledge_index_jobs j WHERE j.version_id=v.id AND j.document_id=v.document_id AND j.tenant_id=v.tenant_id
          AND j.vector_namespace IS NOT NULL AND j.vector_collection IS NOT NULL""")
    _policies(True)


def downgrade():
    _policies(False)
    op.execute('ALTER TABLE knowledge_document_versions DROP COLUMN vector_collection')
    op.execute('ALTER TABLE knowledge_document_versions DROP COLUMN vector_namespace')
