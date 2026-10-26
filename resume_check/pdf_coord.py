# -*- coding: utf-8 -*-
"""Extract PDF text with device coordinates (hex + paren strings, cm transform)."""
import zlib, re, sys

def split_objs(data):
    objs = {}
    for m in re.finditer(rb'(\d+)\s+0\s+obj(.*?)endobj', data, re.S):
        raw = m.group(2)
        sm = re.search(rb'stream\r?\n(.*?)endstream', raw, re.S)
        if sm:
            try: objs[int(m.group(1))] = zlib.decompress(sm.group(1))
            except Exception: objs[int(m.group(1))] = sm.group(1)
    return objs

def cmaps(objs):
    res = {}
    for oid, d in objs.items():
        if b'beginbfchar' not in d and b'beginbfrange' not in d:
            continue
        m = {}
        txt = d.decode('latin-1')
        for mm in re.finditer(r'<([0-9A-Fa-f]{2,8})>\s*<([0-9A-Fa-f]{4,8})>', txt):
            src = bytes.fromhex(mm.group(1)); dst = mm.group(2)
            if len(dst) == 4:
                m[src] = chr(int(dst, 16))
            else:
                m[src] = ''.join(chr(int(dst[i:i+4], 16)) for i in range(0, len(dst), 4))
        for mm in re.finditer(r'<([0-9A-Fa-f]{2,8})>\s*<([0-9A-Fa-f]{2,8})>\s*<([0-9A-Fa-f]{4,8})>', txt):
            lo = int(mm.group(1), 16); hi = int(mm.group(2), 16); dst = int(mm.group(3), 16)
            n = len(mm.group(1)) // 2
            for k in range(min(hi - lo + 1, 4096)):
                m[(lo + k).to_bytes(n, 'big')] = chr(dst + k)
        res[oid] = m
    return res

def decode(b, m):
    out = []; i = 0
    while i < len(b):
        ok = False
        for klen in (2, 1):
            if i + klen <= len(b) and b[i:i+klen] in m:
                out.append(m[b[i:i+klen]]); i += klen; ok = True; break
        if not ok:
            out.append('·'); i += 1
    return ''.join(out)

def lit_hex(h):
    return bytes.fromhex(h)

def lit_paren(s):
    out = bytearray(); i = 0
    while i < len(s):
        c = s[i]
        if c == '\\' and i + 1 < len(s):
            n = s[i+1]
            if n in '()\\': out.append(ord(n)); i += 2
            elif n in 'nrtbf': out.append(ord(n)); i += 2
            elif n.isdigit():
                j = i+1
                while j < len(s) and s[j].isdigit() and j < i+4: j += 1
                out.append(int(s[i+1:j], 8) & 0xff); i = j
            else: out.append(ord(n)); i += 2
        else:
            out.append(ord(c)); i += 1
    return bytes(out)

def main(path):
    data = open(path, 'rb').read()
    objs = split_objs(data)
    cm = cmaps(objs)
    allmap = {}
    for m in cm.values():
        allmap.update(m)

    items = []
    for oid, d in sorted(objs.items()):
        if b'BT' not in d:
            continue
        t = d.decode('latin-1')
        # find first cm matrix
        cmm = re.search(r'([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+cm', t)
        A, B, C, D, E, F = 1.0, 0.0, 0.0, 1.0, 0.0, 0.0
        if cmm:
            A,B,C,D,E,F = (float(cmm.group(i)) for i in range(1,7))
        x = y = 0.0
        # iterate operators in order
        for tok in re.finditer(r'([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+Tm'
                               r'|([-\d.]+)\s+([-\d.]+)\s+(Td|TD)'
                               r'|\[\s*(.*?)\s*\]\s*TJ'
                               r'|<([0-9A-Fa-f]+)>\s*Tj'
                               r'|\((.*?)\)\s*Tj', t, re.S):
            g = tok.groups()
            if g[0] is not None:
                x = float(g[4]); y = float(g[5])
            elif g[6] is not None:
                x += float(g[6]); y += float(g[7])
            elif g[9] is not None:  # TJ array
                arr = g[9]
                parts = re.findall(r'<([0-9A-Fa-f]+)>|\(((?:[^()\\]|\\.)*)\)', arr)
                s = ''
                for h, p in parts:
                    s += decode(lit_hex(h), allmap) if h else decode(lit_paren(p), allmap)
                if s.strip():
                    items.append((x, y, s))
            elif g[10] is not None:  # hex Tj
                s = decode(lit_hex(g[10]), allmap)
                if s.strip():
                    items.append((x, y, s))
            elif g[11] is not None:  # paren Tj
                s = decode(lit_paren(g[11]), allmap)
                if s.strip():
                    items.append((x, y, s))
    # map to device coords
    out = []
    for x, y, s in items:
        dx = E + A * x + C * y
        dy = F + B * x + D * y
        out.append((dy, dx, s))
    out.sort(key=lambda r: (-r[0], r[1]))
    for dy, dx, s in out:
        print(f"y={dy:7.1f} x={dx:6.1f}  {s}")

if __name__ == '__main__':
    main(sys.argv[1])
