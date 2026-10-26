# -*- coding: utf-8 -*-
"""Minimal PDF text extractor: content streams + ToUnicode CMaps, reading order."""
import zlib, re, sys

def get_streams(data):
    out = []
    for m in re.finditer(rb'(\d+)\s+0\s+obj(.*?)endobj', data, re.S):
        obj = m.group(2)
        sm = re.search(rb'stream\r?\n(.*?)endstream', obj, re.S)
        if not sm:
            continue
        try:
            out.append((int(m.group(1)), zlib.decompress(sm.group(1))))
        except Exception:
            out.append((int(m.group(1)), sm.group(1)))
    return dict(out)

def get_tounicode(streams):
    # returns dict: font_res_name -> {byte->unicode str}
    maps = {}
    for objid, d in streams.items():
        if b'beginbfchar' not in d and b'beginbfrange' not in d:
            continue
        m = {}
        txt = d.decode('latin-1')
        for mm in re.finditer(r'<([0-9A-Fa-f]{2,8})>\s*<([0-9A-Fa-f]{4,8})>', txt):
            src = mm.group(1); dst = mm.group(2)
            src_bytes = bytes.fromhex(src)
            if len(dst) == 4:
                m[src_bytes] = chr(int(dst, 16))
            else:
                # surrogate pair
                cp = []
                for i in range(0, len(dst), 4):
                    cp.append(int(dst[i:i+4], 16))
                try:
                    m[src_bytes] = ''.join(chr(c) for c in cp)
                except Exception:
                    pass
        for mm in re.finditer(r'<([0-9A-Fa-f]{2,8})>\s*<([0-9A-Fa-f]{2,8})>\s*<([0-9A-Fa-f]{4,8})>', txt):
            lo = int(mm.group(1), 16); hi = int(mm.group(2), 16); dst = int(mm.group(3), 16)
            for k in range(min(hi - lo + 1, 4096)):
                src = (lo + k).to_bytes(len(mm.group(1)) // 2, 'big')
                m[src] = chr(dst + k)
        maps[objid] = m
    return maps

def main(path):
    data = open(path, 'rb').read()
    streams = get_streams(data)
    tounicode = get_tounicode(streams)

    # find content stream(s) of the page(s): collect all streams with BT
    lines = []
    for objid, d in streams.items():
        if b'BT' not in d:
            continue
        # need font map for this content stream: parse resources
        txt = d.decode('latin-1')
        # gather font references: /F3 12 Tf style; need resource dict /F3 -> font obj -> toUnicode
        # For simplicity: tokenize text-showing ops
        # find Tf mappings and Tj/TJ strings
        # We'll extract strings and map using any cid->unicode table we have (per font if resolvable)
        # Build font dict from content stream's preceding resource info by scanning /F<n> usage
        # Fallback: build global cid->unicode by merging all ToUnicode maps (Word PDFs use one font per subset)
        global_map = {}
        for m in tounicode.values():
            global_map.update(m)
        for tm in re.finditer(r'\[(.*?)\]\s*TJ|\((.*?)\)\s*Tj', txt, re.S):
            arr = tm.group(1)
            if arr is not None:
                parts = re.findall(r'\(((?:[^()\\]|\\.)*)\)', arr)
                for p in parts:
                    s = decode_str(p)
                    lines.append(('TJ', s))
            else:
                s = decode_str(tm.group(2))
                lines.append(('Tj', s))
    # Now decode each string's bytes via global_map
    for i, (kind, s) in enumerate(lines):
        # s is raw bytes string; map via global_map
        if kind == 'Tj' and len(s) > 0:
            out = decode_bytes_to_text(s.encode('latin-1'), global_map)
            print(f"{out}")

def decode_str(s):
    # turn escape sequences into bytes
    s = s.replace(r'\(', '(').replace(r'\)', ')').replace(r'\\', '\\')
    # just return as-is; treat as latin-1 later
    return s

def decode_bytes_to_text(b, m):
    out = []
    i = 0
    while i < len(b):
        matched = False
        for klen in (2, 1):
            if i + klen <= len(b):
                key = b[i:i+klen]
                if key in m:
                    out.append(m[key])
                    i += klen
                    matched = True
                    break
        if not matched:
            # fallback: latin-1 single byte
            out.append(chr(b[i]) if b[i] >= 32 else ' ')
            i += 1
    return ''.join(out)

if __name__ == '__main__':
    main(sys.argv[1])
