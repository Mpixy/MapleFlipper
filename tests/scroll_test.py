"""Same shop, list scrolled: the app must read the new page within a couple of seconds.

    xvfb-run python tests/scroll_test.py pageA.png pageB.png
"""
import os
import sys
import time

sys.argv = [sys.argv[0]] + sys.argv[1:]
here = os.path.dirname(__file__)
src = open(os.path.join(here, "smoke_test.py")).read()
src = src.split("def script():")[0]  # reuse the harness (fake screen, stubs, app)
exec(compile(src, "smoke_test_harness", "exec"))


def script():  # noqa: F811
    time.sleep(1)
    want = [expected(img) for img in screens]
    current["img"] = screens[0]
    wait_for(lambda: len(app.seen) >= 4, 15, "page A read")
    btn = mf.find_shop_button(screens[0][:, :, ::-1])
    current["cursor"] = (int(btn[0]) - 200, int(btn[1]) + 250)  # mouse resting on the list
    current["background"] = (0, 300, 75, 500)  # moving map left of the shop window
    new = {"meshcap"}
    current["img"] = screens[1]  # scrolled one row: a new item at the bottom
    ok = wait_for(lambda: any(min(mf.Levenshtein.distance(mf.norm(n), w) for w in new) <= 2
                              for n in raw_names()), 10, f"new row {sorted(new)} read after scroll")
    if not ok:
        print("    got ", sorted(mf.norm(n) for n in raw_names()))
    app.root.after(0, app.root.destroy)


threading.Thread(target=script, daemon=True).start()
app.run()
print("PASSED" if not fails else f"FAILED: {fails}")
