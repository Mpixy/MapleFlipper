"""The whole app, headless, walking through the shops in tests/shots as if you opened them
one after another (with the mouse resting on the list for a moment). Checks the list shows
each shop's rows, and that nothing lands in the error log.

    xvfb-run python tests/live_test.py
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

os.environ.setdefault("LOCALAPPDATA", tempfile.mkdtemp())
ctypes.windll = types.SimpleNamespace(  # Windows API stand-ins
    shcore=types.SimpleNamespace(SetProcessDpiAwareness=lambda *_: 0),
    user32=types.SimpleNamespace(GetAsyncKeyState=lambda *_: 0, GetCursorPos=lambda *_: 0, GetParent=lambda *_: 0,
                                 GetWindowLongW=lambda *_: 0, SetWindowLongW=lambda *_: 0))
sys.modules["winsound"] = types.SimpleNamespace(MessageBeep=lambda *_: None, MB_ICONASTERISK=0, PlaySound=lambda *_: None,
                                                SND_FILENAME=0, SND_ASYNC=0, SND_NODEFAULT=0)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import maple_flipper as mf  # noqa: E402
from read_test import SHOPS, HERE, same  # noqa: E402

order = os.environ.get("ORDER", "7.png 8.png 9.png 11.png 13.png 10.png 14.png 5.png 9.png 7.png").split()
current = {"img": None}


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
        img = img[region["top"]:region["top"] + region["height"], region["left"]:region["left"] + region["width"]]
    return np.ascontiguousarray(img)


def load(name):
    return np.array(Image.open(os.path.join(HERE, name)).convert("RGB"))[:, :, ::-1].copy()


current["img"] = load(order[0])
mf.mss.mss = FakeMss
mf.grab = fake_grab
mf.App.overlaps_me = lambda self, box: False
mf.App.update_db = lambda self: None
mf.App.check_update = lambda self: None
app = mf.App()
app.cfg["seen_help"] = True
fails = []


def on_page():
    return [(app.seen[k][1], app.seen[k][3]) for k in app.page_order if k in app.seen]


def script():
    time.sleep(2)
    blank = np.full_like(current["img"], 40)
    for name in order:
        current["img"] = blank  # close the shop first (the app has to notice)
        time.sleep(1.5)
        current["img"] = load(name)
        real, may_miss = SHOPS[name]
        t = time.time()
        while time.time() - t < 20:
            got = on_page()
            left = list(real)
            wrong = [g for g in got if not any(r[1] == g[1] and same(g[0], r[0]) for r in left)]
            for g in got:
                hit = next((r for r in left if r[1] == g[1] and same(g[0], r[0])), None)
                if hit:
                    left.remove(hit)
            if app.cur_shop is not None and not wrong and len(left) <= may_miss and (got or not real) \
                    and time.time() - t > 2:
                break
            time.sleep(0.2)
        ok = not wrong and len(left) <= may_miss
        print(f"{'ok  ' if ok else 'FAIL'} {name} ({time.time() - t:.1f}s): {len(got)}/{len(real)}"
              + (f" WRONG {wrong}" if wrong else "") + (f" missed {left}" if left else ""))
        if not ok:
            fails.append(name)
    log = os.path.join(os.environ["LOCALAPPDATA"], "MapleFlipper", "error_log.txt")
    if os.path.exists(log):
        fails.append("errors logged")
        print(open(log).read()[-3000:])
    app.root.after(0, app.root.destroy)


threading.Thread(target=script, daemon=True).start()
app.run()
print("PASSED" if not fails else f"FAILED: {fails}")
sys.exit(1 if fails else 0)
