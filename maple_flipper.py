"""MapleFlipper - finds player-shop items you can flip to an NPC for profit,
and tracks your inventory mesos to confirm the profit.

Prices come from MeowDB (meowdb.com/msclassic), cached locally in prices.json.
Screen reading is done with OCR only; nothing is sent to the game.
"""
import concurrent.futures as cf
import ctypes
import html as htmllib
import json
import os
import re
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox

import mss
import numpy as np
import requests
from rapidfuzz.distance import Levenshtein

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # real pixels on scaled displays
except Exception:
    pass

VERSION = "0.18"
HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(os.environ.get("LOCALAPPDATA", HERE), "MapleFlipper")
os.makedirs(DATA, exist_ok=True)
PRICES_FILE = os.path.join(DATA, "prices.json")
CUSTOM_FILE = os.path.join(DATA, "custom_prices.txt")
CONFIG_FILE = os.path.join(DATA, "config.json")
LOG_FILE = os.path.join(DATA, "mesos_log.csv")
BUNDLED_PRICES = os.path.join(getattr(sys, "_MEIPASS", HERE), "prices.json")
ITEM_URL = "https://meowdb.com/msclassic/item-db/{}"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"}

PRICE_LINE = re.compile(r"^\s*(\d[\d,.]*)\s*f[o0]r\s*(\d[\d,.]*)\s*m", re.I)


def to_int(s):
    digits = re.sub(r"[^\d]", "", s)
    return int(digits) if digits else None


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


def log_error(etype, value, tb):
    """pythonw has no console, so keep crashes in a file we can look at."""
    import traceback
    with open(os.path.join(DATA, "error_log.txt"), "a", encoding="utf-8") as f:
        f.write(time.strftime("\n%Y-%m-%d %H:%M:%S\n") + "".join(traceback.format_exception(etype, value, tb)))


sys.excepthook = log_error
threading.excepthook = lambda a: log_error(a.exc_type, a.exc_value, a.exc_traceback)


# ---------------------------------------------------------------- price database

def parse_item_page(text):
    """Return (name, sellback) from a MeowDB item page, or None."""
    m = re.search(r"NPC\s*Sell-?back(?:\s*\(per unit\))?\s*:?\s*([\d,]+)\s*meso", text, re.I)
    if not m:
        return None
    name = None
    for pat in (r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"',
                r"<h1[^>]*>(.*?)</h1>", r"<title>(.*?)</title>"):
        n = re.search(pat, text, re.I | re.S)
        if n:
            name = re.sub(r"<[^>]+>", "", htmllib.unescape(n.group(1)))
            name = re.split(r"\s+[|\-–·]\s+", name)[0].strip()
            if name and name.lower() not in ("meowdb", "maplestory classic"):
                break
            name = None
    return (name, to_int(m.group(1))) if name else None


def fetch_item(session, item_id):
    for attempt in range(4):
        try:
            r = session.get(ITEM_URL.format(item_id), headers=HEADERS, timeout=20)
        except requests.RequestException:
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code in (429, 500, 502, 503, 504):  # rate limited / busy: wait and retry
            time.sleep(3 * (attempt + 1))
            continue
        if r.status_code != 200:
            return item_id, f"http{r.status_code}"
        text = re.sub(r"<[^>]+>", " ", r.text)  # flatten tags so "Sell-back</span><span>1,800" matches
        parsed = parse_item_page(r.text[:20000] + " " + text)
        return item_id, parsed or "noprice"
    return item_id, "error"


def update_prices(progress, max_id=12000, path=PRICES_FILE):
    """Crawl MeowDB item pages by number. Ids are not contiguous, so scan a wide range."""
    db = load_json(path, {})
    session = requests.Session()
    state_file = path + ".progress"
    state = load_json(state_file, {"next": 1, "found": 0, "misses": 0})  # resume a cut-off download
    first, found, misses, stats = state["next"], state["found"], state["misses"], {}
    t0 = time.time()
    with cf.ThreadPoolExecutor(6) as pool:
        for start in range(first, max_id, 50):
            for iid, res in pool.map(lambda i: fetch_item(session, i), range(start, start + 50)):
                if isinstance(res, tuple):
                    db[res[0]] = res[1]
                    found, misses = found + 1, 0
                else:
                    stats[res] = stats.get(res, 0) + 1
                    misses += 1
            done = start + 49
            left = (time.time() - t0) / max(done - first + 1, 1) * (max_id - done)
            progress(f"Downloading prices... {done * 100 // max_id}%  ({done:,}/{max_id:,} checked, "
                     f"{found:,} items found)  ~{int(left // 60)}:{int(left % 60):02d} left at most", done / max_id)
            if found and misses > 2500:
                break  # well past the last item
            if start % 1000 == 951:  # save every 1000 ids so a cut-off download can resume
                save_json(path, db)
                save_json(state_file, {"next": start + 50, "found": found, "misses": misses})
    with open(os.path.join(DATA, "update_log.txt"), "w", encoding="utf-8") as f:
        f.write(f"found={found} total={len(db)} misses={stats}\n")
    if db:
        save_json(path, db)
    if os.path.exists(state_file):
        os.remove(state_file)
    return found, len(db)


RELEASE_PRICES = "https://github.com/Mpixy/MapleFlipper/releases/download/latest/prices.json"


def download_release_prices():
    """Ready-made price list built by GitHub (one small download instead of crawling MeowDB)."""
    r = requests.get(RELEASE_PRICES, headers=HEADERS, timeout=30)
    r.raise_for_status()
    db = r.json()
    if len(db) < 500:
        raise ValueError("price list too small")
    save_json(PRICES_FILE, db)
    if os.path.exists(PRICES_FILE + ".progress"):
        os.remove(PRICES_FILE + ".progress")
    return len(db)


def load_prices():
    db = load_json(PRICES_FILE, {})
    if len(db) < 500:  # nothing downloaded yet: start from the prices shipped with the app
        db = {**load_json(BUNDLED_PRICES, {}), **db}
    # custom_prices.txt lines: Item Name = 1800
    if os.path.exists(CUSTOM_FILE):
        with open(CUSTOM_FILE, encoding="utf-8") as f:
            for line in f:
                if "=" in line and not line.strip().startswith("#"):
                    name, val = line.rsplit("=", 1)
                    if to_int(val) is not None:
                        db[name.strip()] = to_int(val)
    return db


# ---------------------------------------------------------------- OCR

_ocr = None


def ocr(img):
    global _ocr
    if _ocr is None:
        from rapidocr_onnxruntime import RapidOCR
        try:
            _ocr = RapidOCR(intra_op_num_threads=2, inter_op_num_threads=1)
        except TypeError:
            _ocr = RapidOCR()
    res, _ = _ocr(img)
    out = []
    for box, text, score in res or []:
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        out.append({"text": text.strip(), "x": min(xs), "y": (min(ys) + max(ys)) / 2,
                    "h": max(ys) - min(ys)})
    return out


def grab(region=None):
    with mss.mss() as s:
        mon = region or s.monitors[0]
        shot = np.array(s.grab(mon))[:, :, :3]
    # upscale small regions so the digits read reliably
    if region and shot.shape[0] < 80:
        shot = np.repeat(np.repeat(shot, 3, axis=0), 3, axis=1)
    return np.ascontiguousarray(shot)


def find_shop_items(lines):
    """Pair each 'N for X mesos' line with the item-name line right above it."""
    items = []
    for ln in lines:
        m = PRICE_LINE.search(ln["text"])
        if not m:
            continue
        qty, price = to_int(m.group(1)), to_int(m.group(2))
        if not qty or not price:
            continue
        above = [o for o in lines if o is not ln and abs(o["x"] - ln["x"]) < 40
                 and 0 < ln["y"] - o["y"] < 2.5 * max(ln["h"], 12)
                 and not PRICE_LINE.search(o["text"])]
        if above:
            name = max(above, key=lambda o: o["y"])["text"]
            items.append((name, qty, price))
    return items


def norm(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


def npc_value(name, unit_price, qty):
    """Stars/bullets are one rechargeable item: the shop shows the ammo count, not a stack."""
    if any(k in norm(name) for k in ("throwingstar", "bullet")):
        return unit_price
    return unit_price * qty


def match_name(name, names):
    """Exact item only. Allows 1 OCR typo (2 for long names); anything more is another item."""
    truncated = name.rstrip().endswith((".", "…"))
    key = norm(name.rstrip(".… "))
    if not key:
        return None, 0
    by_key = {}
    for n in names:
        by_key.setdefault(norm(n), n)
    if truncated:  # shop cuts long names: "Earring LUK Scroll: Inter..."
        pre = {k for k in by_key if k.startswith(key)}
        return (by_key[pre.pop()], 95) if len(pre) == 1 else (None, 0)
    if key in by_key:
        return by_key[key], 100
    allowed = 2 if len(key) >= 15 else 1
    close = [(Levenshtein.distance(key, k), k) for k in by_key
             if abs(len(k) - len(key)) <= allowed]
    close = sorted(c for c in close if c[0] <= allowed)
    if close and (len(close) == 1 or close[0][0] < close[1][0]):
        return by_key[close[0][1]], 90
    return None, 0


# ---------------------------------------------------------------- region picker

def pick_region(root, callback):
    with mss.mss() as s:
        mon = s.monitors[0]
    top = tk.Toplevel(root)
    top.overrideredirect(True)
    top.geometry(f"{mon['width']}x{mon['height']}+{mon['left']}+{mon['top']}")
    top.attributes("-alpha", 0.3)
    top.attributes("-topmost", True)
    c = tk.Canvas(top, bg="black", cursor="cross", highlightthickness=0)
    c.pack(fill="both", expand=True)
    c.create_text(mon["width"] // 2, 60, fill="white", font=("Segoe UI", 20, "bold"),
                  text="Drag a box around the MESOS number in your inventory (Esc = cancel)")
    st = {}

    def down(e):
        st["x"], st["y"] = e.x, e.y
        st["r"] = c.create_rectangle(e.x, e.y, e.x, e.y, outline="red", width=3)

    def move(e):
        c.coords(st["r"], st["x"], st["y"], e.x, e.y)

    def up(e):
        x1, x2 = sorted((st["x"], e.x))
        y1, y2 = sorted((st["y"], e.y))
        top.destroy()
        if x2 - x1 > 5 and y2 - y1 > 5:
            callback({"left": x1 + mon["left"], "top": y1 + mon["top"],
                      "width": x2 - x1, "height": y2 - y1})

    c.bind("<Button-1>", down)
    c.bind("<B1-Motion>", move)
    c.bind("<ButtonRelease-1>", up)
    top.bind("<Escape>", lambda e: top.destroy())
    top.focus_force()


# ---------------------------------------------------------------- app

HELP_TEXT = """FIND PROFITABLE ITEMS
1. Walk into a player shop in the game.
2. Press F8 (or the big SCAN button).
3. Green rows = buy it and sell it to any NPC for profit.
4. Scroll down in the shop and press F8 again - the list keeps growing.
5. 'Clear list' before visiting the next shop.

TRACK YOUR MESOS
1. Open your inventory in the game.
2. Click 'Set mesos area' and drag a box around the mesos number.
3. Click 'Start tracking'. Every buy / NPC sale is logged with the profit.

Orange '(?)' = no price known for that item.
Double-click any row to look the item up on MeowDB.
Min profit = only highlight items that earn at least this much.
'Update prices' = refresh prices after a game patch."""


class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title(f"MapleFlipper v{VERSION}")
        self.root.attributes("-topmost", True)
        self.root.geometry("560x620")
        self.cfg = load_json(CONFIG_FILE, {})
        if self.cfg.get("geometry"):
            self.root.geometry(self.cfg["geometry"])
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.report_callback_exception = lambda *exc: log_error(*exc)
        self.prices = load_prices()
        self.names = list(self.prices)
        self.seen = {}
        self.tracking = False
        self.mesos = self.start_mesos = None
        self.busy = self.updating = False
        self.build_ui()
        self.root.bind_all("<F8>", lambda e: self.scan())
        self.start_hotkey()
        if len(self.prices) < 500 or os.path.exists(PRICES_FILE + ".progress"):  # first run / cut off
            self.root.after(300, self.update_db)
        else:
            self.say(f"Ready - {len(self.prices):,} item prices loaded. Open a player shop and press F8.")
        if not self.cfg.get("seen_help"):
            self.cfg["seen_help"] = True
            save_json(CONFIG_FILE, self.cfg)
            self.root.after(500, self.show_help)

    # ---- UI
    def build_ui(self):
        r = self.root
        style = ttk.Style()
        style.configure("Big.TButton", font=("Segoe UI", 14, "bold"), padding=10)
        ttk.Label(r, text=f"v{VERSION}", font=("Segoe UI", 9, "bold"),
                  foreground="#666").pack(anchor="w", padx=8, pady=(4, 0))
        ttk.Button(r, text="🔍  SCAN SHOP  (F8)", style="Big.TButton",
                   command=self.scan).pack(fill="x", padx=8, pady=(8, 4))

        bar = ttk.Frame(r)
        bar.pack(fill="x", padx=8)
        ttk.Button(bar, text="Clear list", command=self.clear).pack(side="left")
        self.only_profit = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="Only profitable", variable=self.only_profit,
                        command=self.refresh).pack(side="left", padx=8)
        ttk.Label(bar, text="Min profit:").pack(side="left")
        self.min_profit = tk.StringVar(value=str(self.cfg.get("min_profit", 0)))
        e = ttk.Entry(bar, textvariable=self.min_profit, width=8)
        e.pack(side="left", padx=(2, 8))
        e.bind("<KeyRelease>", lambda ev: self.refresh())
        self.beep = tk.BooleanVar(value=self.cfg.get("beep", True))
        ttk.Checkbutton(bar, text="Beep", variable=self.beep).pack(side="left")
        ttk.Button(bar, text="Update prices", command=self.update_db).pack(side="right")
        ttk.Button(bar, text="? Help", command=self.show_help).pack(side="right", padx=6)

        cols = ("item", "qty", "price", "npc", "profit")
        self.tree = ttk.Treeview(r, columns=cols, show="headings", height=14)
        for c, w, t in zip(cols, (210, 45, 90, 90, 90),
                           ("Item", "Qty", "Shop price", "NPC pays", "Profit")):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="w" if c == "item" else "e")
        self.tree.tag_configure("good", background="#b8f5b0")
        self.tree.tag_configure("bad", foreground="#999")
        self.tree.tag_configure("unknown", foreground="#c07000")
        self.tree.pack(fill="both", expand=True, padx=8, pady=6)
        self.tree.bind("<Double-1>", self.open_item)

        box = ttk.LabelFrame(r, text="Mesos tracker")
        box.pack(fill="x", padx=8, pady=4)
        row = ttk.Frame(box)
        row.pack(fill="x", padx=4, pady=4)
        ttk.Button(row, text="Set mesos area", command=self.set_area).pack(side="left")
        self.track_btn = ttk.Button(row, text="▶ Start tracking", command=self.toggle_track)
        self.track_btn.pack(side="left", padx=6)
        self.meso_lbl = ttk.Label(box, font=("Segoe UI", 12, "bold"), text="Mesos: -")
        self.meso_lbl.pack(anchor="w", padx=6)
        self.hist = tk.Listbox(box, height=6, font=("Consolas", 10))
        self.hist.pack(fill="x", padx=4, pady=4)

        self.status = ttk.Label(r, text="Ready. Open a player shop and press SCAN (or F8).",
                                relief="sunken", anchor="w", padding=3)
        self.status.pack(fill="x", side="bottom")
        self.pbar = ttk.Progressbar(r, maximum=1.0)

    def say(self, text, pct=None):
        def ui():
            self.status.config(text=text)
            if pct is None:
                self.pbar.pack_forget()
            else:
                self.pbar.pack(fill="x", side="bottom", padx=8, pady=2, before=self.status)
                self.pbar["value"] = pct
        self.root.after(0, ui)

    def show_help(self):
        messagebox.showinfo("How to use MapleFlipper", HELP_TEXT)

    def start_hotkey(self):
        """Poll the real key state: works even when the game swallows the key press."""
        def loop():
            user32 = ctypes.windll.user32
            was_down = False
            while True:
                down = bool(user32.GetAsyncKeyState(0x77) & 0x8000)  # VK_F8
                if down and not was_down:
                    self.root.after(0, self.scan)
                was_down = down
                time.sleep(0.03)
        threading.Thread(target=loop, daemon=True).start()

    # ---- prices
    def update_db(self):
        if self.updating:
            return
        self.updating = True

        def work():
            try:
                self.say("Downloading the latest price list...", 0.5)
                try:
                    total = download_release_prices()
                    found = total
                except Exception:  # no release yet: fall back to reading MeowDB directly
                    found, total = update_prices(self.say)
                self.prices = load_prices()
                self.names = list(self.prices)
                if found == 0:
                    self.say("Could not read prices from MeowDB. Send update_log.txt (in %LOCALAPPDATA%\\MapleFlipper) to Claude.")
                else:
                    self.say(f"Prices updated: {total} items.")
            except Exception as e:
                log_error(type(e), e, e.__traceback__)
                self.say(f"Price update failed: {e} - progress is saved, it will continue next time.")
            self.updating = False

        threading.Thread(target=work, daemon=True).start()

    # ---- shop scan
    def scan(self):
        if self.busy:
            return
        self.busy = True
        self.say("Scanning...")
        self.root.attributes("-alpha", 0.0)  # don't OCR our own window
        self.root.update()
        time.sleep(0.1)
        try:
            img = grab()
        finally:
            self.root.attributes("-alpha", 1.0)

        def work():
            try:
                items = find_shop_items(ocr(img))
                new_good = False
                for name, qty, price in items:
                    real, score = match_name(name, self.names)
                    key = (real or name, price)
                    v = (real, name, qty, price)
                    p = self.profit(v)
                    if key not in self.seen and p is not None and p > 0 and p >= self.min_p():
                        new_good = True
                    self.seen[key] = v
                if new_good and self.beep.get():
                    import winsound
                    winsound.MessageBeep(winsound.MB_ICONASTERISK)
                self.root.after(0, self.refresh)
                n_good = sum(1 for v in self.seen.values() if self.profit(v) and self.profit(v) > 0)
                self.say(f"Found {len(items)} items on screen. Profitable so far: {n_good}. "
                         f"Scroll the shop and scan again for more.")
            except Exception as e:
                log_error(type(e), e, e.__traceback__)
                self.say(f"Scan failed: {e}")
            self.busy = False

        threading.Thread(target=work, daemon=True).start()

    def profit(self, v):
        real, _, qty, price = v
        if not real:
            return None
        return npc_value(real, self.prices[real], qty) - price

    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        rows = sorted(self.seen.values(), key=lambda v: -(self.profit(v) or -1e12))
        for v in rows:
            real, raw, qty, price = v
            p = self.profit(v)
            if self.only_profit.get() and (p is None or p <= 0 or p < self.min_p()):
                continue
            if p is None:
                vals, tag = (f"{raw} (?)", qty, f"{price:,}", "unknown", "-"), "unknown"
            else:
                vals = (real, qty, f"{price:,}", f"{npc_value(real, self.prices[real], qty):,}", f"{p:+,}")
                tag = "good" if p > 0 and p >= self.min_p() else "bad"
            self.tree.insert("", "end", values=vals, tags=(tag,))

    def min_p(self):
        return to_int(self.min_profit.get()) or 0

    def open_item(self, _event):
        sel = self.tree.selection()
        if sel:
            import urllib.parse
            import webbrowser
            name = self.tree.item(sel[0], "values")[0].replace(" (?)", "")
            webbrowser.open("https://www.google.com/search?q=" +
                            urllib.parse.quote(f"site:meowdb.com/msclassic {name}"))

    def on_close(self):
        self.cfg.update(geometry=self.root.geometry(), min_profit=self.min_p(), beep=self.beep.get())
        save_json(CONFIG_FILE, self.cfg)
        self.root.destroy()

    def clear(self):
        self.seen.clear()
        self.refresh()

    # ---- mesos tracker
    def set_area(self):
        def done(region):
            self.cfg["meso_area"] = region
            save_json(CONFIG_FILE, self.cfg)
            v = self.read_mesos()
            self.say(f"Mesos area saved. Read: {v:,}" if v is not None
                     else "Mesos area saved, but no number was read - try a slightly bigger box.")
        pick_region(self.root, done)

    def read_mesos(self, img=None):
        area = self.cfg.get("meso_area")
        if not area:
            return None
        nums = [to_int(o["text"]) for o in ocr(grab(area) if img is None else img)]
        nums = [n for n in nums if n is not None]
        return max(nums) if nums else None

    def toggle_track(self):
        if not self.cfg.get("meso_area"):
            messagebox.showinfo("MapleFlipper", "First click 'Set mesos area' and drag a box "
                                "around the mesos number in your inventory window.")
            return
        self.tracking = not self.tracking
        self.track_btn.config(text="■ Stop tracking" if self.tracking else "▶ Start tracking")
        if self.tracking:
            self.start_mesos = self.mesos = None
            self.hist.delete(0, "end")
            threading.Thread(target=self.track_loop, daemon=True).start()

    def track_loop(self):
        pending, hits, last_img = None, 0, None
        while self.tracking:
            time.sleep(1.0)
            try:
                img = grab(self.cfg["meso_area"])
                if last_img is not None and hits >= 2 and np.array_equal(img, last_img):
                    continue  # nothing changed on screen: skip the expensive OCR
                last_img = img
                v = self.read_mesos(img)
            except Exception:
                v = None
            if v is not None:
                # require 2 identical reads in a row so OCR glitches aren't logged
                if v == pending:
                    hits += 1
                else:
                    pending, hits = v, 1
                if hits == 2 and v != self.mesos:
                    self.root.after(0, self.on_mesos, v)

    def on_mesos(self, v):
        old = self.mesos
        self.mesos = v
        if self.start_mesos is None:
            self.start_mesos = v
        elif old is not None:
            diff = v - old
            kind = "SOLD to NPC / gained" if diff > 0 else "Spent / bought"
            stamp = time.strftime("%H:%M:%S")
            self.hist.insert(0, f"{stamp}  {diff:+12,}  {kind}")
            new_file = not os.path.exists(LOG_FILE)
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                if new_file:
                    f.write("time,change,mesos\n")
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')},{diff},{v}\n")
        total = v - self.start_mesos
        self.meso_lbl.config(text=f"Mesos: {v:,}    Session profit: {total:+,}",
                             foreground="green" if total >= 0 else "red")

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--build-prices":  # used by the release build
        print(update_prices(lambda t, p=None: print(t, flush=True), path=sys.argv[2]))
    else:
        App().run()
