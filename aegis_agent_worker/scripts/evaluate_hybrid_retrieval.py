"""E04真实检索消融；仅随机实验schema/集合，生产SQL/Embedding治理/Qdrant/RRF。"""
import argparse
from contextlib import asynccontextmanager
from dataclasses import asdict, replace
from datetime import datetime
from hashlib import sha256
import json
from importlib.metadata import version
import platform
from pathlib import Path
import re
import sys
from time import perf_counter
from urllib.parse import urlparse
from uuid import uuid4

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/"src"))
from retrieval_eval.dataset import load_dataset, identifier
from retrieval_eval.metrics import ranking_metrics, summarize, paired_intervals, excludes_target
from aegis_agent_worker.config.database import Database
from aegis_agent_worker.config.settings import get_settings
from aegis_agent_worker.embedding.client import EmbeddingClient
from aegis_agent_worker.harness.embedding_executor import EmbeddingExecutor, EmbeddingSubject
from aegis_agent_worker.repository.knowledge_retrieval_repository import KnowledgeRetrievalRepository
from aegis_agent_worker.service.knowledge.hybrid_retrieval_service import HybridRetrievalService
from aegis_agent_worker.agent.contracts.knowledge import KnowledgeQueryPlan
from aegis_agent_worker.indexing.chunker import search_tokens
from aegis_agent_worker.retrieval.policy import RetrievalPolicy
from aegis_agent_worker.retrieval.ranking import ordered_hits
from aegis_agent_worker.tool.contracts import ToolContext, ToolError
from aegis_agent_worker.vector.qdrant_index import QdrantVectorIndex
from aegis_agent_worker.vector.qdrant_search import QdrantSearch
from aegis_agent_worker.vector.contracts import VectorIndexError


def emit(stage, **fields):
    print(json.dumps(dict(stage=stage, **fields), ensure_ascii=False, default=str), flush=True)


class IsolatedDatabase:
    def __init__(self, settings, schema):
        if not re.fullmatch(r"aegis_e04_[a-f0-9]{32}", schema): raise ValueError("实验schema不合法")
        # 不设置public回退：漏建表应失败，而非读写业务表。
        self.engine = create_async_engine(settings.database_async_url, hide_parameters=True,
            connect_args={"server_settings": {"search_path": schema+",pg_catalog"}})
        self.factory = async_sessionmaker(self.engine, expire_on_commit=False)

    @asynccontextmanager
    async def session(self):
        async with self.factory() as session: yield session

    async def execute(self, statement, **parameters):
        async with self.session() as session:
            async with session.begin():
                result = await session.execute(text(statement), parameters)
                return [dict(row) for row in result.mappings()] if result.returns_rows else []


class ObservedRepository(KnowledgeRetrievalRepository):
    async def scope(self, *args):
        result = await super().scope(*args)
        self.versions = [row["version_id"] for row in result]
        return result

    async def lexical(self, *args):
        self.lexical_hits = await super().lexical(*args)
        return self.lexical_hits


class ObservedVector(QdrantSearch):
    async def search(self, *args):
        self.hits = await super().search(*args)
        return self.hits


class UnavailableVector:
    def __init__(self, code): self.code = code
    async def search(self, *args): raise VectorIndexError(self.code, "E04受控故障注入")


async def create_schema(admin, schema, experiment_id, data_hash):
    tables = ("knowledge_documents", "knowledge_document_versions", "knowledge_chunks",
              "knowledge_index_jobs", "knowledge_vector_spaces", "embedding_execution_budgets", "embedding_execution_attempts")
    async with admin.engine.begin() as conn:
        existing = await conn.scalar(text("SELECT count(*) FROM pg_namespace WHERE nspname=:schema"), dict(schema=schema))
        if existing: raise ValueError("实验schema已存在，拒绝接管")
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        await conn.execute(text(f'CREATE TABLE "{schema}".e04_owner(id uuid PRIMARY KEY,data_hash text NOT NULL)'))
        await conn.execute(text(f'INSERT INTO "{schema}".e04_owner VALUES(:id,:hash)'), dict(id=experiment_id, hash=data_hash))
        for table in tables:
            await conn.execute(text(f'CREATE TABLE "{schema}".{table} (LIKE public.{table} INCLUDING ALL)'))
        # EmbeddingRepository使用真实SQL主体/原子预算，但不是主图/Checkpoint实验。
        await conn.execute(text(f'CREATE TABLE "{schema}".agent_runs('
            'id uuid PRIMARY KEY,tenant_id uuid NOT NULL,user_id uuid NOT NULL,status text NOT NULL)'))


async def seed(db, data, chunks, settings, profile, namespace, tenant, user, other_tenant, other_user):
    docs = {item["id"]: item for item in data["documents"]+data["canaries"]}
    for key, doc in docs.items():
        doc["tenant"] = other_tenant if doc.get("state") == "foreign_tenant" else tenant
        doc["owner"] = other_user if doc.get("state") == "other_user_private" else user
        await db.execute("""INSERT INTO knowledge_documents(id,tenant_id,user_id,title,category,visibility,
            external_embedding_allowed,enabled) VALUES(:id,:tenant,:user,:title,:category,'private',true,true)""",
            id=identifier(key), tenant=doc["tenant"], user=doc["owner"], title=doc["title"], category=doc.get("category", "policy"))
    for chunk in chunks:
        chunk["version_key"] = "C06.current" if chunk["key"] == "C06.current" else chunk["document_id"]
    versions = {}
    for chunk in chunks:
        versions.setdefault(chunk["version_key"], []).append(chunk)
    for key, batch in versions.items():
        doc = docs[batch[0]["document_id"]]
        source = "\n\n".join(chunk["content"] for chunk in batch)
        params = dict(version=identifier(key+".version"), doc=identifier(batch[0]["document_id"]),
            tenant=doc["tenant"], provider=profile.provider, model=profile.model, model_version=profile.version,
            dimensions=profile.dimensions, namespace=namespace, collection=settings.qdrant_collection)
        await db.execute("""INSERT INTO knowledge_document_versions(id,document_id,tenant_id,version_no,version_label,
            source_text,content_hash,status,embedding_provider,embedding_model,embedding_model_version,embedding_dimensions,
            vector_namespace,vector_collection) VALUES(:version,:doc,:tenant,:number,:label,:source,:hash,'pending',
            :provider,:model,:model_version,:dimensions,:namespace,:collection)""", **params,
            number=2 if key == "C06.current" else 1, label="e04-v2" if key == "C06.current" else "e04-v1",
            source=source, hash=sha256(source.encode()).hexdigest())
        await db.execute("""INSERT INTO knowledge_index_jobs(id,tenant_id,user_id,document_id,version_id,status)
            VALUES(:job,:tenant,:user,:doc,:version,'running')""", job=identifier(key+".job"),
            tenant=doc["tenant"], user=doc["owner"], doc=params["doc"], version=params["version"])
        offset = 0
        for chunk in batch:
            await db.execute("""INSERT INTO knowledge_chunks(id,document_id,tenant_id,version_id,position,section,content,
                start_offset,end_offset,content_hash,embedding_provider,embedding_model,embedding_model_version,
                embedding_dimensions,search_tokens) VALUES(:id,:doc,:tenant,:version,:position,:section,:content,:start,
                :end,:hash,:provider,:model,:model_version,:dimensions,:tokens)""", **{k:v for k,v in params.items() if k not in {"namespace", "collection"}},
                id=identifier(chunk["key"]), position=chunk["position"], section=chunk["section"], content=chunk["content"],
                start=offset, end=offset+len(chunk["content"]), hash=sha256(chunk["content"].encode()).hexdigest(), tokens=search_tokens(chunk["content"]))
            offset += len(chunk["content"])+2
    await db.execute("""INSERT INTO knowledge_vector_spaces(namespace,collection_name,model_provider,model_name,model_version,dimensions)
        VALUES(:namespace,:collection,:provider,:model,:version,:dimensions)""", namespace=namespace,
        collection=settings.qdrant_collection, provider=profile.provider, model=profile.model, version=profile.version, dimensions=profile.dimensions)
    return docs, versions


async def index_all(db, docs, versions, executor, index, profile):
    for key, chunks in versions.items():
        doc = docs[chunks[0]["document_id"]]
        vectors = await executor.embed(EmbeddingSubject("job", identifier(key+".job"), doc["tenant"], doc["owner"]),
                                       [chunk["content"] for chunk in chunks])
        await index.upsert([dict(id=str(identifier(chunk["key"])), vector=vector,
            payload=dict(tenant_id=str(doc["tenant"]), document_id=str(identifier(chunk["document_id"])),
                version_id=str(identifier(key+".version")), model_provider=profile.provider, model=profile.model,
                model_version=profile.version, dimensions=profile.dimensions)) for chunk, vector in zip(chunks, vectors)])
        await db.execute("UPDATE knowledge_chunks SET indexed_at=now() WHERE version_id=:id", id=identifier(key+".version"))
        await db.execute("UPDATE knowledge_document_versions SET status='ready' WHERE id=:id", id=identifier(key+".version"))
        await db.execute("UPDATE knowledge_index_jobs SET status='succeeded' WHERE id=:id", id=identifier(key+".job"))
        emit("indexed", version=key, chunks=len(chunks))
    for key, doc in docs.items():
        await db.execute("UPDATE knowledge_documents SET current_version_id=:version WHERE id=:id",
            id=identifier(key), version=identifier(("C06.current" if key == "C06" else key)+".version"))
        if doc.get("state") == "disabled": await db.execute("UPDATE knowledge_documents SET enabled=false WHERE id=:id", id=identifier(key))
        if doc.get("state") == "not_ready": await db.execute("UPDATE knowledge_document_versions SET status='indexing' WHERE id=:id", id=identifier(key+".version"))
        if doc.get("state") == "deleted": await db.execute("UPDATE knowledge_documents SET deleted_at=now() WHERE id=:id", id=identifier(key))


async def query_context(db, experiment, key, tenant, user):
    run = identifier(str(experiment)+key)
    await db.execute("INSERT INTO agent_runs(id,tenant_id,user_id,status) VALUES(:id,:tenant,:user,'running')",
                     id=run, tenant=tenant, user=user)
    return ToolContext(tenant, user, run)


async def evaluate_queries(db, data, queries, chunks, executor, vector, settings, policy, experiment, tenant, user):
    repo = ObservedRepository(db)
    service = HybridRetrievalService(settings, repo, executor, vector, policy)
    names = {identifier(chunk["key"]): chunk["key"] for chunk in chunks}
    rows = []
    for query in queries:
        context = await query_context(db, experiment, query["id"], tenant, user)
        repo.lexical_hits, vector.hits = [], []
        start = perf_counter()
        result = await service.search(context, KnowledgeQueryPlan(query=query["query"], top_k=5))
        ms = (perf_counter()-start)*1000
        lists = dict(lexical=ordered_hits(repo.lexical_hits, policy.lexical_candidates),
                     vector=ordered_hits(vector.hits, policy.vector_candidates))
        rankings = dict(rrf=[names[item.chunk_id] for item in result.evidence])
        for arm, hits in lists.items():
            fresh = {item.chunk_id for item in await repo.load(context, repo.versions, [hit.chunk_id for hit in hits])}
            rankings[arm] = [names[hit.chunk_id] for hit in hits if hit.chunk_id in fresh][:5]
        forbidden = {identifier(item["id"]+".version") for item in data["canaries"]}
        raw_ids = {names[hit.chunk_id] for hits in lists.values() for hit in hits}
        leaked = sorted(key for key in raw_ids | {key for ranking in rankings.values() for key in ranking}
                        if key.startswith("C") and key != "C06.current")
        row = {key: query[key] for key in ("id", "document_id", "split", "kind")}
        row.update(query_id=row.pop("id"), rankings=rankings, qrels=query["qrels"],
            metrics={arm: ranking_metrics(ranking, query["qrels"]) for arm, ranking in rankings.items()} if query["qrels"] else {},
            hybrid_ms=ms, mode=result.retrieval.mode, degraded=result.retrieval.degraded,
            forbidden_versions_in_scope=len(forbidden & set(repo.versions)), leaked_canaries=leaked,
            candidates={arm: [dict(chunk=names[hit.chunk_id], score=hit.score) for hit in hits] for arm, hits in lists.items()})
        rows.append(row)
        await db.execute("UPDATE agent_runs SET status='completed' WHERE id=:id", id=context.run_id)
        emit("query", query_id=query["id"], kind=query["kind"], leaked_canaries=len(leaked))
    # 即使直接提交隐藏版本/片段ID，证据SQL仍必须拒绝；不依赖上层scope遮挡。
    direct = []
    for item in data["canaries"]:
        evidence = await repo.load(context, [identifier(item["id"]+".version")], [identifier(item["id"]+".rule")])
        direct.append(dict(case=item["id"], passed=not evidence))
    return rows, direct


async def governance_checks(db, settings, executor, vector, policy, experiment, tenant, user, other_user):
    repo = KnowledgeRetrievalRepository(db)
    service = HybridRetrievalService(settings, repo, executor, vector, policy)
    checks = []
    changes = (("sensitive", "knowledge_documents", "is_sensitive=true", "is_sensitive=false", "D07"),
        ("external_revoked", "knowledge_documents", "external_embedding_allowed=false", "external_embedding_allowed=true", "D07"),
        ("disabled", "knowledge_documents", "enabled=false", "enabled=true", "D07"),
        ("deleted", "knowledge_documents", "deleted_at=now()", "deleted_at=NULL", "D07"),
        ("not_ready", "knowledge_document_versions", "status='indexing'", "status='ready'", "D07.version"),
        ("no_current_version", "knowledge_documents", "current_version_id=NULL", "current_version_id=:version", "D07"),
        ("private_revoked", "knowledge_documents", "user_id=:other", "user_id=:user", "D07"),
        ("chunk_not_indexed", "knowledge_chunks", "indexed_at=NULL", "indexed_at=now()", "D07.rule"))
    for key, table, update, restore, identifier_key in changes:
        await db.execute(f"UPDATE {table} SET {update} WHERE id=:id", id=identifier(identifier_key), other=other_user)
        try:
            context = await query_context(db, experiment, "guard-"+key, tenant, user)
            result = await service.search(context, KnowledgeQueryPlan(query="国内一线城市住宿每晚上限，展会无房如何申请超标？"))
            evidence = await repo.load(context, [identifier("D07.version")], [identifier("D07.rule")])
            checks.append(dict(case=key, passed=not evidence and excludes_target(result.evidence, identifier("D07"),
                identifier("D07.rule") if key == "chunk_not_indexed" else None)))
        finally:
            await db.execute(f"UPDATE {table} SET {restore} WHERE id=:id", id=identifier(identifier_key),
                version=identifier("D07.version"), user=user)
    empty_context = await query_context(db, experiment, "empty-scope", uuid4(), uuid4())
    empty = await service.search(empty_context, KnowledgeQueryPlan(query="差旅费用"))
    checks.append(dict(case="empty_scope", passed=not empty.evidence and empty.retrieval.reason == "no_accessible_documents"))
    faults = []
    for key, code, fallback in (("unavailable_denied", "QDRANT_UNAVAILABLE", False),
                               ("unavailable_fallback", "QDRANT_UNAVAILABLE", True),
                               ("timeout_not_fallback", "QDRANT_TIMEOUT", True)):
        context = await query_context(db, experiment, key, tenant, user)
        injected = HybridRetrievalService(settings, repo, executor, UnavailableVector(code), replace(policy, allow_lexical_fallback=fallback))
        try:
            result = await injected.search(context, KnowledgeQueryPlan(query="出差预算超过5000元审批"))
        except ToolError as error:
            faults.append(dict(case=key, passed=key != "unavailable_fallback" and error.code == code, code=error.code))
        else:
            faults.append(dict(case=key, passed=key == "unavailable_fallback" and result.retrieval.mode == "lexical_only"
                and result.retrieval.degraded and result.retrieval.reason == "vector_unavailable", mode=result.retrieval.mode))
    return checks, faults


async def run(args):
    data, queries, chunks = load_dataset(args.dataset)
    data_hash = sha256(args.dataset.read_bytes()).hexdigest()
    settings = get_settings()
    if not settings.embedding_external_queries_allowed: raise ValueError("必须已有查询外发授权，脚本不改.env")
    experiment = uuid4()
    schema, collection = "aegis_e04_"+experiment.hex, "aegis_e04_"+experiment.hex
    if collection == settings.qdrant_collection: raise ValueError("不能使用业务集合")
    settings = settings.model_copy(update={"qdrant_collection": collection, "database_echo": False})
    admin, db = Database(settings), None
    quota = dict(embedding_requests=0, qdrant_requests=0)
    async def gate(request):
        name, maximum = ("embedding_requests", args.max_embedding_calls) if request.url.path.endswith("/embeddings") else ("qdrant_requests", 250)
        if quota[name] >= maximum: raise RuntimeError("E04_REQUEST_LIMIT")
        quota[name] += 1
    report = dict(version="e04-report-v1", dataset_sha256=data_hash, dataset_version=data["version"],
        source_sha256=sha256(b"".join(p.relative_to(ROOT/"src").as_posix().encode()+p.read_bytes()
                                    for p in sorted((ROOT/"src").rglob("*.py")))).hexdigest(),
        evaluator_sha256=sha256(Path(__file__).read_bytes()).hexdigest(),
        python=platform.python_version(), dependencies={name:version(name) for name in ("sqlalchemy","httpx","pydantic")},
        experiment_id=str(experiment), started_at=datetime.now().astimezone().isoformat(),
        resources=dict(schema=schema, collection=collection), complete=False, cleanup=False,
        scope="real_pg_predicates_embedding_executor_qdrant_rrf_not_import_or_answer_generation",
        rls_tested=False, schema_contract="LIKE public INCLUDING ALL does not clone foreign keys or RLS; no roles changed",
        policy=asdict(RetrievalPolicy()), document_count=30, chunk_count=79, query_count=60)
    remote_owned, schema_owned, stage = False, False, "setup"
    emit("resources", **report["resources"], dataset_sha256=data_hash)
    async with httpx.AsyncClient(event_hooks={"request": [gate]}) as http:
        try:
            stage = "schema"
            await create_schema(admin, schema, experiment, data_hash)
            schema_owned = True
            db = IsolatedDatabase(settings, schema)
            index = QdrantVectorIndex(settings, http)
            if await index._request("GET", "", allow_missing=True) is not None: raise ValueError("随机集合已存在，拒绝接管")
            remote_owned = True
            stage = "collection"
            client = EmbeddingClient(settings, http)
            profile = client.profile
            await index.ensure_collection(profile)
            tenant, user, other_tenant, other_user = (uuid4() for _ in range(4))
            executor = EmbeddingExecutor(db, client, external_queries_allowed=settings.embedding_external_queries_allowed)
            report["profile"] = dict(provider=profile.provider, model=profile.model, version=profile.version, dimensions=profile.dimensions,
                embedding_host=urlparse(str(settings.embedding_api_base)).hostname, qdrant_host=urlparse(str(settings.qdrant_url)).hostname)
            stage = "seed"
            docs, versions = await seed(db, data, chunks, settings, profile, index.namespace, tenant, user, other_tenant, other_user)
            stage = "index"
            await index_all(db, docs, versions, executor, index, profile)
            policy = RetrievalPolicy()
            vector = ObservedVector(settings, http)
            stage = "queries"
            rows, direct = await evaluate_queries(db, data, queries, chunks, executor, vector, settings, policy, experiment, tenant, user)
            stage = "governance"
            checks, faults = await governance_checks(db, settings, executor, vector, policy, experiment, tenant, user, other_user)
            report.update(results=rows, direct_source_checks=direct, governance_checks=checks, injected_vector_faults=faults,
                dev=summarize(rows, "dev"), test=summarize(rows, "test"), paired_bootstrap=paired_intervals(rows),
                no_answer={arm: dict(queries=6, returned_candidates=sum(bool(row["rankings"][arm]) for row in rows if row["kind"] == "no_answer"))
                           for arm in ("lexical", "vector", "rrf")},
                audit=await db.execute("""SELECT purpose,status,count(*) AS calls,sum(input_chars) AS input_chars,
                    sum(total_tokens) AS total_tokens,count(*) FILTER(WHERE total_tokens IS NULL) AS unknown_usage
                    FROM embedding_execution_attempts GROUP BY purpose,status ORDER BY purpose,status"""))
            report["complete"] = all(item["passed"] for item in direct+checks+faults) and all(
                not row["leaked_canaries"] and row["forbidden_versions_in_scope"] == 0 for row in rows)
            # 仅完整实验清理自己创建且有所有权标记的精确资源，失败保留供诊断。
            if report["complete"]:
                stage = "cleanup"
                if not remote_owned or not schema_owned: raise ValueError("没有实验资源所有权")
                marker = await db.execute("SELECT id,data_hash FROM e04_owner")
                if marker != [dict(id=experiment, data_hash=data_hash)]: raise ValueError("资源所有权标记不匹配")
                report.update(requests=quota)
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
                await index._request("DELETE", "")
                await db.engine.dispose()
                async with admin.engine.begin() as conn:
                    await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
                report["cleanup"] = True
        except Exception as error:
            report.update(error_stage=stage, error_code=getattr(error, "code", type(error).__name__))
            emit("failed", stage_name=stage, code=report["error_code"], **report["resources"])
        finally:
            report.update(requests=quota, finished_at=datetime.now().astimezone().isoformat())
            if db is not None: await db.engine.dispose()
            await admin.dispose()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    emit("finished", complete=report["complete"], cleanup=report["cleanup"], requests=quota, report=args.output)
    return 0 if report["complete"] and report["cleanup"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=ROOT/"tests/evaluation/data/hybrid_retrieval_e04_v1.json")
    parser.add_argument("--allow-external-embedding", action="store_true", required=True)
    parser.add_argument("--allow-isolated-resources", action="store_true", required=True)
    parser.add_argument("--max-embedding-calls", type=int, default=120)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.max_embedding_calls <= 120 or args.output.exists(): parser.error("额度超限或输出已存在")
    import asyncio
    raise SystemExit(asyncio.run(run(args)))
