# -*- coding: utf-8 -*-
"""Extract clean logical text lines from a textbox-based DOCX (skip mc:Fallback duplicates)."""
import sys, zipfile
from lxml import etree

W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
MC_NS = 'http://schemas.openxmlformats.org/markup-compatibility/2006'

path = sys.argv[1]
z = zipfile.ZipFile(path)
root = etree.fromstring(z.read('word/document.xml'))
body = root.find(W + 'body')
lines = []

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
            parts = []
            for p in ch.findall(W + 'p'):
                t = ''.join(x.text or '' for x in p.iter(W + 't'))
                parts.append(t)
            txt = '\n'.join(parts)
            if txt.strip():
                lines.append(txt.strip())
            continue
        walk(ch)

walk(body)
for i, l in enumerate(lines):
    print(f"[{i:03d}] {l}")
print(f"\n--- {len(lines)} logical text boxes ---")
