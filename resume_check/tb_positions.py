# -*- coding: utf-8 -*-
"""Extract each textbox's absolute position (EMU) + first text, to reconstruct visual layout."""
import sys, zipfile
from lxml import etree

W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
WP = '{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}'
A = '{http://schemas.openxmlformats.org/drawingml/2006/main}'
MC_NS = 'http://schemas.openxmlformats.org/markup-compatibility/2006'
EMU_PER_CM = 360000

path = sys.argv[1]
z = zipfile.ZipFile(path)
root = etree.fromstring(z.read('word/document.xml'))
body = root.find(W + 'body')

def split_tag(tag):
    if not isinstance(tag, str):
        return '', ''
    if tag.startswith('{'):
        ns, local = tag[1:].split('}', 1)
        return ns, local
    return '', tag

entries = []

def walk(el):
    for ch in el:
        ns, local = split_tag(ch.tag)
        if ns == MC_NS and local == 'Fallback':
            continue
        if local == 'txbxContent':
            # find the containing anchor's offsets
            # look upward: this txbxContent is inside a wp:anchor or wp:inline
            anc = ch
            posH = posV = None
            a = ch
            while a is not None:
                a = a.getparent()
                if a is None:
                    break
                ans, alocal = split_tag(a.tag)
                if ans == WP and alocal == 'anchor':
                    hp = a.find(WP + 'positionH')
                    vp = a.find(WP + 'positionV')
                    if hp is not None:
                        posH = hp.find(A + 'posOffset')
                    if vp is not None:
                        posV = vp.find(A + 'posOffset')
                    break
                if ans == WP and alocal == 'inline':
                    break
            text = ''
            for p in ch.findall(W + 'p'):
                t = ''.join(x.text or '' for x in p.iter(W + 't'))
                if t.strip():
                    text = t.strip()
                    break
            if text:
                entries.append((posH, posV, text))
            continue
        walk(ch)

walk(body)

def val(e):
    return int(e.text) if e is not None else None

for posH, posV, text in entries:
    x = val(posH); y = val(posV)
    xs = f"{x/EMU_PER_CM:6.2f}cm" if x is not None else "  ?"
    ys = f"{y/EMU_PER_CM:6.2f}cm" if y is not None else "  ?"
    print(f"x={xs} y={ys}  {text[:52]}")
