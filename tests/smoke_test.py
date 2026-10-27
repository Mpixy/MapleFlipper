"""Headless end-to-end test: runs the real app window against screenshots instead of the screen.

    xvfb-run python tests/smoke_test.py shop1.png shop2.png

Shows shop1, waits for auto-detection + scan, then swaps to shop2 (another shop / scrolled
page) and checks the list follows. Windows-only calls are stubbed.
"""
import ctypes
import os
import sys
import tempfile
import threading
import time
import types

import numpy as np
from PIL import Image

os.environ["LOCALAPPDATA"] = tempfile.mkdtemp()
ctypes.windll = types.SimpleNamespace(  # Windows API stand-ins
    shcore=types.SimpleNamespace(SetProcessDpiAwareness=lambda *_: 0),
    user32=types.SimpleNamespace(GetAsyncKeyState=lambda *_: 0, GetParent=lambda *_: 0,
                                 GetWindowLongW=lambda *_: 0, SetWindowLongW=lambda *_: 0))
sys.modules["winsound"] = types.SimpleNamespace(MessageBeep=lambda *_: None, MB_ICONASTERISK=0)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import maple_flipper as mf  # noqa: E402

screens = [np.array(Image.open(p).convert("RGB"))[:, :, ::-1].copy() for p in sys.argv[1:]]
current = {"img": screens[0]}


class FakeMss:
    def __enter__(self):
        h, w = current["img"].shape[:2]
        self.monitors = [{"left": 0, "top": 0, "width": w, "height": h}]
        return self

    def __exit__(self, *a):
        pass


def fake_grab(region=None):
    img = current["img"]
    if region:
        img = img[region["top"]:region["top"] + region["height"],
                  region["left"]:region["left"] + region["width"]]
    return np.ascontiguousarray(img)


mf.mss.mss = FakeMss
mf.grab = fake_grab
mf.App.overlaps_me = lambda self, box: False

app = mf.App()
app.cfg["seen_help"] = True
app.prices = {"Iron Ore": 20}
fails = []


def wait_for(cond, secs, what):
    t = time.time()
    while time.time() - t < secs:
        if cond():
            print(f"OK  {what} ({time.time() - t:.1f}s)")
            return True
        time.sleep(0.1)
    fails.append(what)
    print(f"FAIL {what}")
    return False


def raw_names():
    return {v[1] for v in app.seen.values()}


def expected(img):
    """What a plain full-screen read of this screenshot finds."""
    return {mf.norm(n) for n, _, _ in mf.find_shop_items(mf.ocr(img))}


def close_enough(got, want):
    """Same items, allowing a letter or two of OCR difference per name."""
    if len(got) != len(want):
        return False
    return all(any(mf.Levenshtein.distance(g, w) <= 2 for w in want) for g in got)


def script():
    time.sleep(1)
    want = [expected(img) for img in screens]
    for i, img in enumerate(screens):
        current["img"] = img
        ok = wait_for(lambda: close_enough({mf.norm(n) for n in raw_names()}, want[i]), 15,
                      f"screen {i + 1}: list shows exactly this shop")
        if not ok:
            print("    got ", sorted(mf.norm(n) for n in raw_names()))
            print("    want", sorted(want[i]))
    log = os.path.join(os.environ["LOCALAPPDATA"], "MapleFlipper", "error_log.txt")
    if os.path.exists(log):
        fails.append("errors logged")
        print(open(log).read())
    app.root.after(0, app.root.destroy)


threading.Thread(target=script, daemon=True).start()
app.run()
print("PASSED" if not fails else f"FAILED: {fails}")
sys.exit(1 if fails else 0)
