"""Quick checks of the app's logic (no screen, no window).

    python tests/logic_test.py
"""
import os
import random
import sys
import tempfile
import time
import types

os.environ.setdefault("LOCALAPPDATA", tempfile.mkdtemp())
try:
    import tkinter  # noqa: F401
except ImportError:
    tk = types.ModuleType("tkinter")
    tk.ttk = tk.messagebox = types.ModuleType("x")
    sys.modules["tkinter"] = tk
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import maple_flipper as mf  # noqa: E402

fails = []


def check(what, ok):
    print(("ok   " if ok else "FAIL ") + what)
    if not ok:
        fails.append(what)


check("two items sold together are found", mf.sale_combos([100, 250, 40, 7], 290) == [(1, 2)])
check("the same price twice still adds up", mf.sale_combos([250, 250, 40], 500) == [(0, 1)])
# a long 'to sell' list froze the window for minutes (2026-11-03 crash_log): must stay instant
random.seed(1)
prices = [random.choice([40, 50, 80, 100, 120, 500, 1305, 2000, 3750, 4500, 7500, 11800]) for _ in range(150)]
for total in (123457, 999999, sum(prices[:6]), 17):
    t = time.time()
    mf.sale_combos(prices, total)
    check(f"150 items, total {total}: under 0.5 s ({time.time() - t:.3f} s)", time.time() - t < 0.5)
print("all good" if not fails else f"{len(fails)} failed")
sys.exit(1 if fails else 0)
