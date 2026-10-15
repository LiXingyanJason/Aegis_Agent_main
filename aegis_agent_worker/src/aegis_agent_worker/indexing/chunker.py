"""保留原字符位置的确定性段落切块；中文检索文本采用有界字/二元词组。"""
from dataclasses import dataclass
from hashlib import sha256
import re
from uuid import uuid5, UUID

PARSER_VERSION = 'paragraph-v1'


@dataclass(frozen=True,slots=True)
class Chunk:
    id: UUID
    position: int
    section: str | None
    content: str
    start_offset: int
    end_offset: int
    content_hash: str
    search_tokens: str


def search_tokens(text):
    """为 PostgreSQL 的关键词检索准备文本：
    它提取英文、数字等词项，并把连续中文拆成单字和相邻双字，最后去重。"""
    terms = re.findall(r'[A-Za-z0-9_@.\-]+',text.lower())
    for run in re.findall(r'[\u4e00-\u9fff]+',text):
        terms.extend(run)
        terms.extend(run[index:index+2] for index in range(len(run)-1))
    return ' '.join(dict.fromkeys(terms))


def chunk_text(text, version_id, *, size=700, overlap=80, maximum=512):
    """把一份资料切成多个片段，默认每片约 700 字符、相邻片重叠 80 字符，并尽量在段落空行处切开。
    每片会记录正文、所属 Markdown 标题、原文位置、内容哈希和检索词项。
    片段 ID 由版本、位置和内容确定，因此任务重试时能识别已经处理过的片段。"""
    if not text.strip() or '\0' in text or size>800 or size<=overlap or overlap<0:
        raise ValueError('非法切块输入或配置')
    chunks, start, section = [],0,None
    while start<len(text):
        end = min(start+size,len(text))
        if end<len(text):
            boundary = text.rfind('\n\n',start+size//2,end)
            if boundary>start:
                end=boundary+2
        piece=text[start:end]
        headings=re.findall(r'^#{1,6}\s+(.+)$',piece,re.M)
        if headings:
            section=headings[-1][:200]
        if piece.strip():
            digest=sha256(piece.encode()).hexdigest()
            chunks.append(Chunk(uuid5(version_id,f'{PARSER_VERSION}:{start}:{end}:{digest}'),len(chunks),section,
                                piece,start,end,digest,search_tokens(piece)))
            if len(chunks)>maximum:
                raise ValueError('片段数量超限')
        if end==len(text):
            break
        start=max(start+1,end-overlap)
    return chunks
