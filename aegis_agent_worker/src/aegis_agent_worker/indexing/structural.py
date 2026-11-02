"""Opt-in structure-v1 candidate. Original Unicode slices only; no network/DB."""
from dataclasses import dataclass
from hashlib import sha256
import re

from .contracts import RawChunk, RetrievalText, SourceRange, SourceScope

PARSER_VERSION = 'structure-v1'
HEADING = re.compile(r'^(#{1,6})\s+(.+?)\s*#*\s*$')
CLAUSE = re.compile(r'^\s*(?:第[一二三四五六七八九十百\d]+条|\d+(?:\.\d+)*[.、\s])')
LIST = re.compile(r'^\s*(?:[-+*]|\d+[.)])\s+')


@dataclass(frozen=True)
class StructuralPolicy:
    size: int = 700
    overlap: int = 80
    maximum: int = 512
    max_source_chars: int = 262144

    def __post_init__(self):
        if not 1 <= self.size <= 800 or not 0 <= self.overlap < self.size:
            raise ValueError('invalid structure limits')
        if not 1 <= self.maximum <= 512 or not 1 <= self.max_source_chars <= 1048576:
            raise ValueError('invalid structure source limits')


@dataclass(frozen=True)
class StructuralIndex:
    chunks: tuple[RawChunk, ...]
    # chunk -> independently referenced original heading/header/lead ranges.
    relations: dict

    def enhanced(self, title):
        return tuple(RetrievalText(chunk_id=c.chunk_id, document_title=title,
            heading_path=c.heading_path, raw_content=c.content).verify_chunk(c) for c in self.chunks)


def _pieces(text, start, end, policy):
    """Sentence/row boundaries preferred; hard cut only when no boundary fits."""
    while start < end:
        stop = min(start + policy.size, end)
        hard = False
        if stop < end:
            matches = list(re.finditer(r'[。！？；]|[.!?;](?:\s|$)|\n', text[start:stop]))
            boundary = start + matches[-1].end() if matches else start
            if boundary > start:
                stop = boundary
            else:
                hard = True
        yield start, stop, hard
        if stop == end:
            break
        # Only reuse complete trailing sentences/rows; never break progress.
        tail = text[start:stop]
        boundaries = [start+m.end() for m in re.finditer(r'[。！？；]|[.!?;](?:\s|$)|\n', tail)]
        next_start = next((b for b in boundaries if stop-policy.overlap <= b < stop), stop)
        start = max(start+1, next_start)


def parse_structure(text, scope: SourceScope, policy=None):
    policy = policy or StructuralPolicy()
    if scope.parser_version != PARSER_VERSION:
        raise ValueError('parser scope mismatch')
    if not text.strip() or '\0' in text or len(text)>policy.max_source_chars:
        raise ValueError('invalid source')
    lines = []
    offset = 0
    for line in text.splitlines(keepends=True):
        lines.append((offset, offset+len(line), line))
        offset += len(line)
    path, headings = [], []
    units = []
    clause = None
    i = 0
    while i < len(lines):
        start, end, line = lines[i]
        stripped = line.strip()
        if not stripped:
            i += 1
            continue
        if stripped.startswith(('```','~~~')):
            marker = stripped[:3]
            j=i+1
            while j<len(lines) and not lines[j][2].strip().startswith(marker):
                j+=1
            j=min(j+1,len(lines))
            units.append((start,lines[j-1][1],'paragraph',tuple(path),clause,()))
            i=j
            continue
        heading = HEADING.match(line.rstrip('\r\n'))
        if heading:
            level = len(heading[1])
            while headings and headings[-1][0] >= level:
                headings.pop()
            headings.append((level,heading[2],SourceRange(start=start,end=end)))
            path = [h[1] for h in headings]
            clause = f'heading:{start}' if CLAUSE.match(heading[2]) else None
            units.append((start,end,'heading',tuple(path),clause,()))
            i += 1
            continue
        kind = 'paragraph'
        refs = []
        if CLAUSE.match(line) and not LIST.match(line):
            clause = f'clause:{start}'
            kind = 'clause'
        elif LIST.match(line):
            kind = 'list'
        elif '|' in line and i+1<len(lines) and re.match(r'^\s*\|?\s*:?-{3}',lines[i+1][2]):
            kind = 'table'
        j = i+1
        if kind == 'table':
            while j<len(lines) and '|' in lines[j][2] and lines[j][2].strip():
                j += 1
            refs.append(SourceRange(start=start,end=lines[min(i+1,j-1)][1]))
        elif kind == 'list':
            if units and units[-1][2] in ('paragraph','clause') and units[-1][3]==tuple(path):
                refs.append(SourceRange(start=units[-1][0],end=units[-1][1]))
            while j<len(lines) and (LIST.match(lines[j][2]) or lines[j][2].startswith(('  ','\t'))) and lines[j][2].strip():
                j += 1
        else:
            while j<len(lines) and lines[j][2].strip():
                nxt = lines[j][2]
                if HEADING.match(nxt.rstrip()) or LIST.match(nxt) or CLAUSE.match(nxt) or nxt.strip().startswith(('```','~~~')):
                    break
                if '|' in nxt:
                    break
                j += 1
        units.append((start,lines[j-1][1],kind,tuple(path),clause,tuple(refs)))
        i = j
    # Merge only adjacent ordinary paragraphs in same section/clause. Never merge
    # lists/tables/clauses and lose their own structure identity.
    merged = []
    for u in units:
        if (merged and u[2]=='paragraph' and merged[-1][2]=='paragraph'
                and u[3:5]==merged[-1][3:5] and u[1]-merged[-1][0]<=policy.size):
            prev = merged.pop()
            merged.append((prev[0],u[1],u[2],u[3],u[4],u[5]))
        else:
            merged.append(u)
    chunks, relations = [], {}
    for start,end,kind,path,clause,refs in merged:
        for a,b,hard in _pieces(text,start,end,policy):
            body = text[a:b]
            c = RawChunk(scope=scope,structure='hard_split' if hard else kind,heading_path=path,
                clause_id=clause,source_range=SourceRange(start=a,end=b),content=body,
                content_hash=sha256(body.encode()).hexdigest()).verify_source(text)
            chunks.append(c)
            relations[c.chunk_id] = refs
            if len(chunks)>policy.maximum:
                raise ValueError('structure chunk limit')
    return StructuralIndex(tuple(chunks),relations)
