# -*- coding: utf-8 -*-
from PIL import Image
import os

src = r"D:\AI\Aegis_Agent_main\resume_check\png_copy_pdf\page-0001.png"
outdir = r"D:\AI\Aegis_Agent_main\resume_check\crops"
os.makedirs(outdir, exist_ok=True)
im = Image.open(src)
W, H = im.size

regions = {
    "edu_right_edge": (600, 262, W, 322, 4),   # right side of education rows: clipping check
    "honors_line": (30, 300, W - 10, 340, 4),  # honors text exact reading
}
for name, (l, t, r, b, z) in regions.items():
    c = im.crop((l, t, r, b))
    c = c.resize((int(c.width * z), int(c.height * z)), Image.LANCZOS)
    p = os.path.join(outdir, f"{name}.png")
    c.save(p)
    print("wrote", p, c.size)
