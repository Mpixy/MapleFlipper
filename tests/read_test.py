"""Reads every screenshot in tests/shots the way the app does (find the shop, read its rows,
skip rows under a tooltip) and checks the result against what is really in the shop.

    python tests/read_test.py

Every row read must be a real row of that shop (right name, right price). Rows hidden by a
tooltip / dialog may be skipped, but no more than `may_miss` per shot.
"""
import ctypes
import os
import sys
import tempfile
import types

os.environ["LOCALAPPDATA"] = tempfile.mkdtemp()
if not hasattr(ctypes, "windll"):
    ctypes.windll = types.SimpleNamespace(shcore=types.SimpleNamespace(SetProcessDpiAwareness=lambda *_: 0))
try:
    import tkinter  # noqa: F401
except ImportError:  # (a server without a display: the reading needs no window)
    tk = types.ModuleType("tkinter")
    tk.ttk = tk.messagebox = types.ModuleType("x")
    sys.modules["tkinter"] = tk
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import cv2  # noqa: E402
import numpy as np  # noqa: E402
import maple_flipper as mf  # noqa: E402
from rapidfuzz.distance import Levenshtein  # noqa: E402

HERE = os.path.join(os.path.dirname(__file__), "shots")
# shot -> (rows that are really there and not greyed out, how many may be hidden by a tooltip / box)
SHOPS = {
    "5.png": ([("Tourist T", 25555), ("Tourist T", 25555), ("Patissier Uniform", 24555),
               ("Prisoner Pants", 22221), ("Beetle Long Sleeve", 22222), ("Orange Baseball", 23333)], 5),
    "7.png": ([("Machete", 5000), ("Pole Arm", 3000), ("Wooden Baseball Bat", 3500),
               ("Dark Master Sergeant", 4000), ("Brown Skullcap", 300)], 0),
    "8.png": ([("Slime Bubble", 50), ("Stiff Feather", 49), ("Fragment of Magic", 68),
               ("Animal Fur Bundle", 35), ("Leather", 94), ("Silver Ore", 88)], 0),
    "9.png": ([("Opal Ore", 100), ("Topaz Ore", 120)], 0),
    "10.png": ([("Overall Armor STR Scroll:...", 14444), ("Earring STR Sc", 64444),
                ("Emerald Earrings", 23333), ("Emerald Earrings", 23333)], 1),
    "11.png": ([("Cape Magic Def. Scroll: L...", 8999), ("Leather", 999), ("Stiff Feather", 199),
                ("Firewood", 199), ("Wand Magic Attack Scroll:...", 149999)], 0),
    "13.png": ([("Green Napoleon", 399999), ("Axe Pole Arm", 24999), ("Axe Pole Arm", 14999),
                ("Cape Weapon Def. Scroll:...", 49999), ("One-Handed Sword Attac...", 7999)], 0),
    "14.png": ([], 0),
}

SHOPS["15.png"] = ([("Earring DEX Scroll: Inter...", 55999), ("Pet Equip Jump Scroll: Gr...", 4999)], 0)
# the list ends on this page (empty slots under the last row): the app may say DONE
ENDS = {"9.png", "10.png", "13.png", "14.png"}


def same(read, real):
    a, b = mf.norm(read.rstrip(".… ")), mf.norm(real.rstrip(".… "))
    return a == b or Levenshtein.distance(a, b) <= max(1, len(b) // 6) or (len(a) >= 8 and b.startswith(a))


def read_shop(img):
    """What the app reads in this shop (as the auto-detect + scan do it)."""
    btn = mf.find_shop_button(img[:, :, ::-1])
    if btn is None:
        return None
    x, y = int(btn[0]), int(btn[1])
    h, w = img.shape[:2]
    cx, cy = max(x - 420, 0), max(y + 80, 0)
    cand = np.ascontiguousarray(img[cy:cy + 520, cx:cx + 580])
    lines = mf.ocr(cand)
    prices = [o for o in lines if mf.PRICE_LINE.search(o["text"])]
    if not prices:
        return []
    # the app's list box: the text columns + the icons
    x0 = min(o["x"] for o in prices) + cx
    y0 = min(o["y"] for o in prices) + cy
    y1 = max(o["y"] for o in prices) + cy
    nb = (int(max(x0 - 92, 0)), int(max(y0 - 55, 0)))
    bw, bh = 420, int(y1 - y0 + 75)
    sub = np.ascontiguousarray(img[nb[1]:nb[1] + bh, nb[0]:nb[0] + bw])
    shifted = [{**o, "x": o["x"] + cx - nb[0], "y": o["y"] + cy - nb[1]} for o in lines]
    rows = mf.shop_rows(shifted)
    got = mf.read_rows(sub, rows, seed=True)
    out = []
    for name, qty, price, (rx, ry, rw, rh), sure in got:
        if not mf.covered(sub, (rx + 50, ry, 230, rh)):
            out.append((name, price))
    return out


def main():
    bad = 0
    for shot, (real, may_miss) in SHOPS.items():
        img = cv2.imread(os.path.join(HERE, shot))
        got = read_shop(img)
        if got is None:
            print(f"FAIL {shot}: the shop window was not found")
            bad += 1
            continue
        left = list(real)
        wrong = []
        for name, price in got:
            hit = next((r for r in left if r[1] == price and same(name, r[0])), None)
            if hit:
                left.remove(hit)
            else:
                wrong.append((name, price))
        ok = not wrong and len(left) <= may_miss
        bad += not ok
        print(f"{'ok  ' if ok else 'FAIL'} {shot}: read {len(got)}/{len(real)}"
              + (f"  WRONG {wrong}" if wrong else "") + (f"  missed {left}" if left else ""))
    print("all good" if not bad else f"{bad} shot(s) failed")
    return bad


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
