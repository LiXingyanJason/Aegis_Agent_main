# -*- coding: utf-8 -*-
from PIL import Image
import os

outdir = r"D:\AI\Aegis_Agent_main\resume_check\crops"
os.makedirs(outdir, exist_ok=True)

jobs = [
    (r"D:\AI\Aegis_Agent_main\resume_check\png_copy_pdf\page-0001.png", "new_edu_block", (30, 240, 1075, 440), 3),
    (r"D:\AI\Aegis_Agent_main\resume_check\png_orig_pdf\page-0001.png", "old_edu_block", (30, 240, 1075, 440), 3),
]
for src, name, box, z in jobs:
    if not os.path.exists(src):
        print("missing", src)
        continue
    im = Image.open(src)
    c = im.crop(box)
    c = c.resize((int(c.width * z), int(c.height * z)), Image.LANCZOS)
    p = os.path.join(outdir, f"{name}.png")
    c.save(p)
    print("wrote", p, c.size)
