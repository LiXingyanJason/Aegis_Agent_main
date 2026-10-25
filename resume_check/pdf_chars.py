# -*- coding: utf-8 -*-
"""Check which Unicode chars are actually rendered in a PDF, via ToUnicode CMaps."""
import zlib, re, sys

def chars_in_pdf(path):
    data = open(path, 'rb').read()
    streams = re.findall(rb'stream\r?\n(.*?)endstream', data, re.S)
    mapped = set()
    n_cmap = 0
    for s in streams:
        try:
            d = zlib.decompress(s)
        except Exception:
            continue
        if b'beginbfchar' not in d and b'beginbfrange' not in d:
            continue
        n_cmap += 1
        txt = d.decode('latin-1')
        # bfchar entries: <src> <dst>
        for m in re.finditer(r'<([0-9A-Fa-f]{4,8})>\s*<([0-9A-Fa-f]{4,8})>', txt):
            dst = m.group(2)
            if len(dst) == 4:
                mapped.add(int(dst, 16))
            elif len(dst) >= 8:
                # surrogate pair
                for i in range(0, len(dst), 4):
                    mapped.add(int(dst[i:i+4], 16))
        # bfrange entries
        for m in re.finditer(r'<([0-9A-Fa-f]{2,8})>\s*<([0-9A-Fa-f]{2,8})>\s*<([0-9A-Fa-f]{2,8})>', txt):
            lo, hi, dst = (int(x, 16) for x in m.groups())
            if len(m.group(3)) == 4:
                for k in range(min(hi - lo + 1, 4096)):
                    mapped.add(dst + k)
    return mapped, n_cmap, len(streams)

for p in sys.argv[1:]:
    mapped, n_cmap, n_streams = chars_in_pdf(p)
    print(f"\n=== {p}")
    print(f"cmaps={n_cmap} streams={n_streams} mapped_chars={len(mapped)}")
    tests = "在读预计硕士研究生本科密码学软件工程云南大学中国科学院MGIINDIN二类会议第一作者学业奖学金"
    missing = [c for c in sorted(set(tests)) if ord(c) not in mapped]
    print("缺失字符:", "".join(missing) if missing else "(无)")
    # show context of 在读
    for ch in "在读（）":
        print(f"  U+{ord(ch):04X} {ch}: {'有' if ord(ch) in mapped else '缺失'}")
