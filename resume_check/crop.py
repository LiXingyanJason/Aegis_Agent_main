# -*- coding: utf-8 -*-
"""Crop regions of the rendered resume for close inspection."""
from PIL import Image
import sys, os

src = r"D:\AI\Aegis_Agent_main\resume_check\png_copy_pdf\page-0001.png"
outdir = r"D:\AI\Aegis_Agent_main\resume_check\crops"
os.makedirs(outdir, exist_ok=True)
im = Image.open(src)
W, H = im.size
print("size", W, H)

regions = {
    "edu_honors": (30, 250, W - 20, 340),
    "proj1_head": (30, 430, W - 20, 545),
    "proj1_bullets": (30, 545, W - 20, 900),
    "proj2_head": (30, 1010, W - 20, 1120),
}
for name, box in regions.items():
    c = im.crop(box)
    c = c.resize((int(c.width * 2), int(c.height * 2)), Image.LANCZOS)
    p = os.path.join(outdir, f"{name}.png")
    c.save(p)
    print("wrote", p, c.size)
