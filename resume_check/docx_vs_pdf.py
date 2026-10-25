# -*- coding: utf-8 -*-
"""Cross-check: which characters present in the DOCX never appear in the exported PDF?"""
import sys, zipfile, re, zlib
from lxml import etree

W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
MC_NS = 'http://schemas.openxmlformats.org/markup-compatibility/2006'

docx, pdf = sys.argv[1], sys.argv[2]

# --- docx text (skip mc:Fallback) ---
z = zipfile.ZipFile(docx)
root = etree.fromstring(z.read('word/document.xml'))
pieces = []

def split_tag(tag):
    if tag.startswith('{'):
        ns, local = tag[1:].split('}', 1)
        return ns, local
    return '', tag

def walk(el):
    for ch in el:
        if not isinstance(ch.tag, str):
            continue
        ns, local = split_tag(ch.tag)
        if ns == MC_NS and local == 'Fallback':
            continue
        if local == 'txbxContent':
            for p in ch.findall(W + 'p'):
                t = ''.join(x.text or '' for x in p.iter(W + 't'))
                if t.strip():
                    pieces.append(t)
            continue
        walk(ch)

walk(root.find(W + 'body'))
doc_text = '\n'.join(pieces)

# --- pdf: collect mapped unicode chars ---
data = open(pdf, 'rb').read()
streams = re.findall(rb'stream\r?\n(.*?)endstream', data, re.S)
mapped = set()
for s in streams:
    try:
        d = zlib.decompress(s)
    except Exception:
        continue
    if b'beginbfchar' not in d and b'beginbfrange' not in d:
        continue
    txt = d.decode('latin-1')
    for m in re.finditer(r'<([0-9A-Fa-f]{4,8})>\s*<([0-9A-Fa-f]{4,8})>', txt):
        dst = m.group(2)
        if len(dst) == 4:
            mapped.add(int(dst, 16))
        elif len(dst) >= 8:
            for i in range(0, len(dst), 4):
                mapped.add(int(dst[i:i+4], 16))
    for m in re.finditer(r'<([0-9A-Fa-f]{2,8})>\s*<([0-9A-Fa-f]{2,8})>\s*<([0-9A-Fa-f]{2,8})>', txt):
        lo, hi, dst = (int(x, 16) for x in m.groups())
        if len(m.group(3)) == 4:
            for k in range(min(hi - lo + 1, 4096)):
                mapped.add(dst + k)

print(f"docx 字符数={len(doc_text)} pdf 映射字符集={len(mapped)}")
missing = {}
for i, ch in enumerate(doc_text):
    if ch in '\n\t\r ':
        continue
    if ord(ch) not in mapped:
        ctx = doc_text[max(0, i - 25):i + 25].replace('\n', ' ⏎ ')
        missing.setdefault(ch, []).append(ctx)

print(f"\n=== DOCX 里有、但 PDF 里查不到的字：{len(missing)} 个 ===")
for ch, ctxs in missing.items():
    print(f"  [{ch}] U+{ord(ch):04X} × {len(ctxs)}")
    for c in ctxs[:3]:
        print(f"        …{c}…")
