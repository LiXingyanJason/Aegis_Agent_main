"""保留首轮原始报告，仅重验E04单片段断言；累计额度不重置，成功后清理原实验资源。"""
import argparse
import asyncio
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import re
from urllib.parse import urlparse
from uuid import UUID

import httpx
from sqlalchemy import text

from evaluate_hybrid_retrieval import (ROOT, IsolatedDatabase, Database, get_settings, query_context,
    EmbeddingClient, EmbeddingExecutor, QdrantVectorIndex, QdrantSearch, HybridRetrievalService,
    KnowledgeRetrievalRepository, KnowledgeQueryPlan, RetrievalPolicy, identifier, emit)
from retrieval_eval.metrics import excludes_target


async def verify(args):
    original = json.loads(args.report.read_text(encoding="utf-8"))
    if original.get("complete") or original.get("error_code"): raise ValueError("只支持完整排名且仅片段断言未通过的原报告")
    failed = [item["case"] for item in original["governance_checks"] if not item["passed"]]
    if failed != ["chunk_not_indexed"]: raise ValueError("不能跳过其他失败")
    if not all(item["passed"] for item in original["direct_source_checks"]+original["injected_vector_faults"]):
        raise ValueError("存在其他边界失败")
    if len(original["results"]) != 60 or any(row["leaked_canaries"] or row["forbidden_versions_in_scope"] for row in original["results"]):
        raise ValueError("原报告范围或安全检查不合格")
    experiment = UUID(original["experiment_id"])
    schema = original["resources"]["schema"]
    collection = original["resources"]["collection"]
    if schema != collection or schema != "aegis_e04_"+experiment.hex or not re.fullmatch(r"aegis_e04_[a-f0-9]{32}", schema):
        raise ValueError("实验资源名称不匹配")
    settings = get_settings()
    if (collection == settings.qdrant_collection or not settings.embedding_external_queries_allowed
        or urlparse(str(settings.qdrant_url)).hostname != original["profile"]["qdrant_host"]
        or urlparse(str(settings.embedding_api_base)).hostname != original["profile"]["embedding_host"]):
        raise ValueError("配置/授权范围变化")
    settings = settings.model_copy(update={"qdrant_collection": collection, "database_echo": False})
    db, admin = IsolatedDatabase(settings, schema), Database(settings)
    quota = dict(original["requests"])
    report = dict(original)
    report["original_governance_checks"] = original["governance_checks"]
    report["verification"] = dict(original_sha256=sha256(args.report.read_bytes()).hexdigest(), original_path=str(args.report),
        reason="原断言错误地将片段indexed_at失效当成整份文档失效；标签/排名/RRF参数未改", dataset_or_rankings_changed=False,
        verifier_sha256=sha256(Path(__file__).read_bytes()).hexdigest(), passed=False)
    async def gate(request):
        name, maximum = ("embedding_requests", 120) if request.url.path.endswith("/embeddings") else ("qdrant_requests", 250)
        if quota[name] >= maximum: raise RuntimeError("E04_REQUEST_LIMIT")
        quota[name] += 1
    try:
        marker = await db.execute("SELECT id,data_hash FROM e04_owner")
        if marker != [dict(id=experiment, data_hash=original["dataset_sha256"])]: raise ValueError("所有权不匹配")
        row = (await db.execute("SELECT tenant_id,user_id FROM knowledge_documents WHERE id=:id", id=identifier("D07")))[0]
        async with httpx.AsyncClient(event_hooks={"request": [gate]}) as http:
            client = EmbeddingClient(settings, http)
            profile = client.profile
            if (profile.model,profile.version,profile.dimensions) != (original["profile"]["model"],original["profile"]["version"],original["profile"]["dimensions"]):
                raise ValueError("模型空间改变")
            executor = EmbeddingExecutor(db, client, external_queries_allowed=settings.embedding_external_queries_allowed)
            repo = KnowledgeRetrievalRepository(db)
            service = HybridRetrievalService(settings, repo, executor, QdrantSearch(settings, http), RetrievalPolicy())
            await db.execute("UPDATE knowledge_chunks SET indexed_at=NULL WHERE id=:id", id=identifier("D07.rule"))
            try:
                context = await query_context(db, experiment, "verify-chunk-only", row["tenant_id"], row["user_id"])
                result = await service.search(context, KnowledgeQueryPlan(query="国内一线城市住宿每晚上限，展会无房如何申请超标？"))
                direct = await repo.load(context, [identifier("D07.version")], [identifier("D07.rule")])
                passed = not direct and excludes_target(result.evidence, identifier("D07"), identifier("D07.rule"))
                report["verification"].update(passed=passed,
                    invalid_chunk_delivered=any(item.chunk_id == identifier("D07.rule") for item in result.evidence),
                    other_valid_chunks_delivered=sum(item.document_id == identifier("D07") for item in result.evidence))
            finally:
                await db.execute("UPDATE knowledge_chunks SET indexed_at=now() WHERE id=:id", id=identifier("D07.rule"))
            if passed:
                report["governance_checks"] = [item if item["case"] != "chunk_not_indexed" else dict(case="chunk_not_indexed", passed=True,
                    verified_by="one_additional_governed_query") for item in original["governance_checks"]]
                report["audit"] = await db.execute("""SELECT purpose,status,count(*) AS calls,sum(input_chars) AS input_chars,
                    sum(total_tokens) AS total_tokens,count(*) FILTER(WHERE total_tokens IS NULL) AS unknown_usage
                    FROM embedding_execution_attempts GROUP BY purpose,status ORDER BY purpose,status""")
                # 先保存统计与审计的独立新产物，不覆盖原报告，再清理精确的所有资源。
                report.update(complete=True, requests=quota)
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
                await QdrantVectorIndex(settings, http)._request("DELETE", "")
                await db.engine.dispose()
                async with admin.engine.begin() as conn:
                    await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
                report["cleanup"] = True
    except Exception as error:
        report["verification"]["error_code"] = getattr(error, "code", type(error).__name__)
    finally:
        await db.engine.dispose()
        await admin.dispose()
    report.update(requests=quota, verified_at=datetime.now().astimezone().isoformat())
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    emit("verified", complete=report["complete"], cleanup=report["cleanup"], verification=report["verification"], requests=quota)
    return 0 if report["complete"] and report["cleanup"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--allow-external-embedding", action="store_true", required=True)
    parser.add_argument("--allow-isolated-resources", action="store_true", required=True)
    args = parser.parse_args()
    if args.output.exists(): parser.error("不得覆盖历史结果")
    raise SystemExit(asyncio.run(verify(args)))
