// 仅解析规范来源编号，不解释Markdown/HTML，不把模型文本变成URL或脚本。
export function citationParts(text, sources = []) {
  const byNumber = new Map(sources.map((source) => [source.source_no, source]))
  return String(text ?? '').split(/(\[[1-5]\])/).filter(Boolean).map((value) => ({
    text: value,
    source: /^\[[1-5]\]$/.test(value) ? byNumber.get(Number(value.slice(1, -1))) ?? null : null,
  }))
}

export function unavailableKnowledgeArtifacts(artifacts, availability = 'unavailable') {
  return artifacts.map((artifact) => artifact.artifact_type === 'knowledge_sources'
    ? { ...artifact, payload: { sources: [], availability, has_conflict: artifact.payload?.has_conflict === true } }
    : artifact)
}

export function sameSourceSnapshot(source, current) {
  const fields = ['document_id', 'version_id', 'chunk_id', 'content_hash', 'title', 'version_label', 'section', 'start_offset', 'end_offset', 'excerpt']
  return Boolean(source && current && fields.every((field) => source[field] === current[field]))
}

// 上传只发送服务端当前允许的元数据，不能再带入旧类别字段。
export function knowledgeImportMetadata(input) {
  return {
    title: input.title,
    client_request_id: input.client_request_id,
    visibility: input.visibility,
    external_embedding_allowed: input.external_embedding_allowed,
    is_sensitive: input.is_sensitive,
  }
}

const retrievalReasons = {
  no_accessible_documents: '当前没有可检索的已授权资料。',
  no_matches: '已授权资料中没有匹配结果。',
  sources_unavailable: '候选来源在交付前已失效。',
  result_limit: '结果超出交付大小限制，未返回来源。',
  vector_unavailable: '向量检索暂不可用，本次仅使用关键词检索。',
}

// RRF 排名分数仅用于内部排序，不能在界面上解释为准确率或可信度。
export function knowledgeRetrievalSummary(output) {
  const retrieval = output?.retrieval
  const count = Number.isInteger(output?.evidence_count) && output.evidence_count >= 0
    ? output.evidence_count : null
  return {
    count,
    mode: retrieval?.mode === 'lexical_only' ? '仅关键词检索' : retrieval?.mode === 'hybrid' ? '混合检索' : '知识检索',
    degraded: retrieval?.degraded === true,
    reason: retrievalReasons[retrieval?.reason] ?? null,
  }
}

const knowledgeErrors = {
  KNOWLEDGE_SCOPE_TOO_LARGE: '可访问资料过多，请缩小范围。',
  KNOWLEDGE_QUERY_LIMIT: '检索词项过多，请缩短或改写问题。',
  EMBEDDING_DATA_POLICY_BLOCKED: '查询外发未获授权，请检查资料及服务配置。',
  EMBEDDING_BUDGET_EXCEEDED: '知识查询预算已用尽。',
  QDRANT_UNAVAILABLE: '向量检索服务暂不可用，请稍后重试。',
  QDRANT_TIMEOUT: '向量检索超时，请稍后重试。',
}

export function knowledgeSearchError(tool) {
  return knowledgeErrors[tool?.error_code] ?? tool?.error_message ?? tool?.error_code ?? '知识检索失败。'
}
