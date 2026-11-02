"""Fixed aliases b/d/v for generation-source authorization, never model SQL."""
GENERATION_SOURCE_STATE='''(
 (v.status='ready' AND d.current_version_id=v.id)
 OR (v.status IN ('pending','indexing','failed') AND (d.current_version_id IS NULL OR d.current_version_id=v.id)
   AND EXISTS(SELECT 1 FROM knowledge_index_jobs aj WHERE aj.generation_id=b.generation_id
     AND aj.tenant_id=b.tenant_id AND aj.user_id=b.user_id AND aj.document_id=d.id AND aj.version_id=v.id
     AND aj.status='running' AND aj.lease_expires_at>now()))
)'''
