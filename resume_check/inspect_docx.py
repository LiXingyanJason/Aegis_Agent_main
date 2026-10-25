# -*- coding: utf-8 -*-
"""Inspect DOCX package parts and extract ALL text in document order (incl. text boxes)."""
import sys, zipfile, re
from lxml import etree

path = sys.argv[1]
W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'

z = zipfile.ZipFile(path)
print("===== PARTS =====")
for n in z.namelist():
    info = z.getinfo(n)
    print(f"  {n:50s} {info.file_size:>10d}")

xml = z.read('word/document.xml')
root = etree.fromstring(xml)
print("\n===== TOP-LEVEL BODY CHILDREN =====")
body = root.find(W + 'body')
for ch in body:
    tag = etree.QName(ch).localname
    desc = ""
    if tag == 'p':
        t = ''.join(x.text or '' for x in ch.iter(W + 't'))
        nb = len(ch.findall('.//' + W + 'txbxContent'))
        drw = len(ch.findall('.//{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}anchor'))
        pict = len(ch.findall('.//' + W + 'pict'))
        alt = len(ch.findall('.//{http://schemas.openxmlformats.org/markup-compatibility/2006}AlternateContent'))
        desc = f"text={t[:60]!r} textboxes={nb} anchors={drw} pict={pict} altcontent={alt}"
    elif tag == 'tbl':
        rows = len(ch.findall(W + 'tr'))
        desc = f"rows={rows} text={''.join(x.text or '' for x in ch.iter(W+'t'))[:80]!r}"
    elif tag == 'sectPr':
        desc = "section properties"
    print(f"  <{tag}> {desc}")

print("\n===== ALL TEXT NODES IN DOCUMENT ORDER =====")
n = 0
for t in root.iter(W + 't'):
    txt = t.text or ''
    if txt.strip():
        # figure out nearest ancestor info
        anc = []
        p = t.getparent()
        while p is not None:
            ln = etree.QName(p).localname
            if ln in ('txbxContent', 'tbl', 'tr', 'tc', 'body', 'drawing', 'pict'):
                anc.append(ln)
            p = p.getparent()
        print(f"[{n:03d}] {'/'.join(anc[:4]):40s} {txt}")
        n += 1
print(f"\ntotal non-empty text nodes: {n}")
