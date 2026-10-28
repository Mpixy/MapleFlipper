"""Run the real reading pipeline on shop screenshots and print what it reads - no window.

    python tests/ocr_check.py shot1.png shot2.webp ...

For each screenshot: the seller, your mesos, and every row (name -> matched item, price, sure).
Use it to check a change against real screenshots before shipping it.
"""
import os
import sys
import tempfile
import time

os.environ["LOCALAPPDATA"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

import maple_flipper as mf  # noqa: E402

prices = mf.load_prices()
names = sorted(prices)
mf.name_check = lambda n: mf.match_name(n, names)[0] is not None
mf.matcher = lambda n: mf.match_name(n, names)
mf.ocr(np.zeros((32, 32, 3), np.uint8))
for path in sys.argv[1:]:
    img = np.ascontiguousarray(np.array(Image.open(path).convert("RGB"))[:, :, ::-1])
    btn = mf.find_shop_button(img[:, :, ::-1])
    if not btn:
        print(f"{os.path.basename(path)}: no player shop found")
        continue
    x, y = int(btn[0]), int(btn[1])
    t = time.time()
    seller, mesos = mf.seller_name(img, btn), mf.shop_mesos(img, btn)
    box = np.ascontiguousarray(img[max(y + 80, 0):y + 600, max(x - 420, 0):x + 160])
    lines = mf.ocr(box)
    got = mf.read_rows(box, mf.shop_rows(lines), seed=True, worth=lambda *a: True)
    print(f"{os.path.basename(path)}: seller {seller!r}  mesos {mesos}  ({time.time() - t:.2f}s)")
    for name, qty, price, _, sure in got:
        real, score = mf.match_name(name, names)
        pays = mf.npc_value(real, prices[real], qty) if real in prices else None
        print(f"    {name!r:34} -> {str(real):30} {qty:>4} for {price:>7,}  NPC {pays if pays is not None else '?':>7}"
              f"  {'sure' if sure else 'CHECK'}")
