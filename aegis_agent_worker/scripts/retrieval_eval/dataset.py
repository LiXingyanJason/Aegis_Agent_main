import json
from pathlib import Path
from uuid import UUID, uuid5

NAMESPACE = UUID("365408ed-9137-46f3-a2dd-5d62141ee92f")


def identifier(key):
    return uuid5(NAMESPACE, key)


def load_dataset(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data["version"] != "hybrid-e04-v1" or len(data["documents"]) != 24 or len(data["canaries"]) != 6:
        raise ValueError("冻结数据集结构不匹配")
    queries, chunks = [], []
    ids = [doc["id"] for doc in data["documents"]+data["canaries"]]
    if len(set(ids)) != len(ids): raise ValueError("文档ID重复")
    for doc in data["documents"]:
        for position, section in enumerate(("rule", "context", "exception")):
            chunks.append(dict(key=doc["id"]+"."+section, document_id=doc["id"], position=position,
                section=section, content=doc["title"]+"\n"+doc[section]))
        for variant in ("direct", "semantic"):
            qrels = {doc["id"]+".rule": 2}
            if variant == "semantic": qrels[doc["id"]+".exception"] = 1
            queries.append(dict(id=doc["id"]+"-"+variant, document_id=doc["id"], split=doc["split"],
                kind="answerable", query=doc["query_"+variant], qrels=qrels))
    for item in data["no_answer_queries"]:
        queries.append(dict(**item, document_id=None, kind="no_answer", qrels={}))
    for item in data["canaries"]:
        chunks.append(dict(key=item["id"]+".rule", document_id=item["id"], position=0,
            section="rule", content=item["title"]+"\n"+item["content"]))
        queries.append(dict(id=item["id"], document_id=item["id"], kind="acl", split="safety", query=item["query"], qrels={}))
    # C06旧版本的向量必须保留；新版本仅含无关占位文本，不能包含旧条款。
    chunks.append(dict(key="C06.current", document_id="C06", position=0, section="current",
        content="蓝桦当前版本说明\n旧的实验门槛条款已撤销；本版本只说明文档负责人和年度复核安排。"))
    if len(queries) != 60 or len(chunks) != 79: raise ValueError("冻结样本数量不匹配")
    return data, queries, chunks
