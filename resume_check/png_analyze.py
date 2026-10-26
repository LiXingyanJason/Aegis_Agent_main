# -*- coding: utf-8 -*-
"""Analyze rendered resume PNG: content extents, bottom/right margins, detect edge clipping."""
from PIL import Image
import sys

src = sys.argv[1]
im = Image.open(src).convert('L')
W, H = im.size
px = im.load()

# find dark pixels (text/ink): threshold
dark = set()
for y in range(H):
    row = []
    for x in range(W):
        if px[x, y] < 120:
            dark.add((x, y))

if not dark:
    print("no dark pixels")
    sys.exit()

min_x = min(x for x, y in dark)
max_x = max(x for x, y in dark)
min_y = min(y for x, y in dark)
max_y = max(y for x, y in dark)
print(f"image {W}x{H}")
print(f"ink bbox: x [{min_x},{max_x}]  y [{min_y},{max_y}]")
print(f"bottom margin: {H - max_y} px  ({H - max_y}/{H})")
print(f"right margin : {W - max_x} px")
print(f"left margin  : {min_x} px")
print(f"top margin   : {min_y} px")

# count dark pixels per column in the right 8% to detect right-edge overflow text
right_zone = int(W * 0.90)
hits = sum(1 for x, y in dark if x > right_zone)
print(f"dark pixels in rightmost 10%: {hits}")

# bottom 4% band
bottom_zone = int(H * 0.96)
bhits = sum(1 for x, y in dark if y > bottom_zone)
print(f"dark pixels in bottom 4%: {bhits}")
