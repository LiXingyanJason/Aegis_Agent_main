# -*- coding: utf-8 -*-
from PIL import Image
import os

src = r"D:\AI\Aegis_Agent_main\resume_check\png_copy_pdf\page-0001.png"
outdir = r"D:\AI\Aegis_Agent_main\resume_check\crops"
os.makedirs(outdir, exist_ok=True)
im = Image.open(src)
W, H = im.size

regions = {
    "p1_bullets2": (30, 890, W - 20, 1015),
    "p2_head": (30, 1130, W - 20, 1215),
    "p2_bullets": (30, 1215, W - 20, 1470),
    "footer_area": (30, 1440, W - 20, H),
    "header_top": (30, 20, W - 20, 240),
}
for name, box in regions.items():
    c = im.crop(box)
    c = c.resize((int(c.width * 2), int(c.height * 2)), Image.LANCZOS)
    p = os.path.join(outdir, f"{name}.png")
    c.save(p)
    print("wrote", p, c.size)
