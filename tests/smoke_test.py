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
    if current.get("cursor"):  # the game's hand cursor animates: changing pixels over the list
        img = img.copy()
        x, y = current["cursor"]
        img[y:y + 32, x:x + 32] = np.random.randint(0, 255, 3)
    if region:
        img = img[region["top"]:region["top"] + region["height"],
                  region["left"]:region["left"] + region["width"]]
    return np.ascontiguousarray(img)


mf.mss.mss = FakeMss
mf.grab = fake_grab
mf.App.overlaps_me = lambda self, box: False
mf.App.update_db = lambda self: None  # use the test prices, no download
mf.App.check_update = lambda self: None

app = mf.App()
app.cfg["seen_help"] = True
app.prices = {"Iron Ore": 20, "Octopus Leg": 12, "Silver Earrings": 50000}  # earrings: +10,001
app.names = sorted(app.prices)
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
    got = got - {"silverearrings"} if "silverearrings" not in want else got  # kept profitable item
    if len(got) != len(want):
        return False
    return all(any(mf.Levenshtein.distance(g, w) <= 2 for w in want) for g in got)


def script():
    time.sleep(1)
    want = [expected(img) for img in screens]
    for i, img in enumerate(screens):
        current["img"] = img
        btn = mf.find_shop_button(img[:, :, ::-1])
        # park an animated cursor on the shop list (like the mouse resting there)
        current["cursor"] = (int(btn[0]) - 200, int(btn[1]) + 250) if i else None
        ok = wait_for(lambda: close_enough({mf.norm(n) for n in raw_names()}, want[i]), 15,
                      f"screen {i + 1}: list shows exactly this shop")
        if not ok:
            print("    got ", sorted(mf.norm(n) for n in raw_names()))
            print("    want", sorted(want[i]))
    # the profitable earrings from shop 1 must still be listed after visiting other shops
    keys = [k for k, v in app.seen.items() if v[0] == "Silver Earrings"]
    if keys and app.seen_shop.get(keys[0]):
        print(f"OK  profitable item kept from shop '{app.seen_shop[keys[0]]}'")
    else:
        fails.append("profitable item kept")
        print("FAIL profitable item kept", app.seen_shop)
    # buying it (mesos drop by its price) removes it from the list
    app.root.after(0, app.on_mesos, 500000)
    time.sleep(0.3)
    app.root.after(0, app.on_mesos, 500000 - 39999)
    wait_for(lambda: not any(v[0] == "Silver Earrings" for v in app.seen.values()), 5,
             "bought item leaves the list and goes to 'to sell'")
    print("   ", app.tosell_lbl.cget("text"))
    app.bought.clear()
    app.mesos = None
    # a flip: buy Octopus Leg (shop 1 sells it for 30), sell it to an NPC for 12
    current["img"], current["cursor"] = screens[0], None
    wait_for(lambda: any(v[0] == "Octopus Leg" for v in app.seen.values()), 15, "back on shop 1")
    for m in (100000, 100000 - 30, 100000 - 30 + 12):
        app.root.after(0, app.on_mesos, m)
        time.sleep(0.3)
    wait_for(lambda: len(app.stats["flips"]) == 1 and not app.bought, 5, "flip recorded from mesos changes")
    print("   ", app.stats["flips"], "|", app.stats_lbl.cget("text"))
    log = os.path.join(os.environ["LOCALAPPDATA"], "MapleFlipper", "error_log.txt")
    if os.path.exists(log):
        fails.append("errors logged")
        print(open(log).read())
    app.root.after(0, app.root.destroy)


threading.Thread(target=script, daemon=True).start()
app.run()
print("PASSED" if not fails else f"FAILED: {fails}")
sys.exit(1 if fails else 0)
