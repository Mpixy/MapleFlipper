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
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox

import mss
import numpy as np
import requests
from rapidfuzz import fuzz, process

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # real pixels on scaled displays
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(os.environ.get("LOCALAPPDATA", HERE), "MapleFlipper")
os.makedirs(DATA, exist_ok=True)
PRICES_FILE = os.path.join(DATA, "prices.json")
CUSTOM_FILE = os.path.join(DATA, "custom_prices.txt")
CONFIG_FILE = os.path.join(DATA, "config.json")
LOG_FILE = os.path.join(DATA, "mesos_log.csv")
ITEM_URL = "https://meowdb.com/msclassic/item-db/{}"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"}

PRICE_LINE = re.compile(r"(\d[\d,.]*)\s*f[o0]r\s*(\d[\d,.]*)\s*m", re.I)


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


def update_prices(progress, max_id=12000):
    """Crawl MeowDB item pages by number. Ids are not contiguous, so scan a wide range."""
    db = load_json(PRICES_FILE, {})
    session = requests.Session()
    found, misses, stats = 0, 0, {}
    t0 = time.time()
    with cf.ThreadPoolExecutor(10) as pool:
        for start in range(1, max_id, 50):
            for iid, res in pool.map(lambda i: fetch_item(session, i), range(start, start + 50)):
                if isinstance(res, tuple):
                    db[res[0]] = res[1]
                    found, misses = found + 1, 0
                else:
                    stats[res] = stats.get(res, 0) + 1
                    misses += 1
            done = start + 49
            left = (time.time() - t0) / done * (max_id - done)
            progress(f"Downloading prices... {done * 100 // max_id}%  ({done:,}/{max_id:,} checked, "
                     f"{found:,} items found)  ~{int(left // 60)}:{int(left % 60):02d} left at most")
            if found and misses > 2500:
                break  # well past the last item
            if found % 500 < 50:
                save_json(PRICES_FILE, db)
    with open(os.path.join(DATA, "update_log.txt"), "w", encoding="utf-8") as f:
        f.write(f"found={found} total={len(db)} misses={stats}\n")
    if db:
        save_json(PRICES_FILE, db)
    return found, len(db)


def load_prices():
    db = load_json(PRICES_FILE, {})
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


def match_name(name, names):
    raw_trunc = name.rstrip().endswith((".", "…"))
    name = name.rstrip(".… ")
    if not name:
        return None, 0
    truncated = raw_trunc
    best = process.extractOne(name, names, scorer=fuzz.ratio, processor=norm)
    if best and best[1] >= (95 if truncated else 88):
        return best[0], best[1]
    if truncated:  # shop cuts long names: "Earring LUK Scroll: Inter..."
        pre = [n for n in names if norm(n).startswith(norm(name))]
        if len(pre) == 1:
            return pre[0], 95
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

class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("MapleFlipper")
        self.root.attributes("-topmost", True)
        self.root.geometry("560x620")
        self.cfg = load_json(CONFIG_FILE, {})
        self.prices = load_prices()
        self.names = list(self.prices)
        self.seen = {}
        self.tracking = False
        self.mesos = self.start_mesos = None
        self.busy = False
        self.build_ui()
        self.root.bind_all("<F8>", lambda e: self.scan())
        self.start_hotkey()
        if len(self.prices) < 500:  # first run or an incomplete download
            self.root.after(300, self.update_db)

    # ---- UI
    def build_ui(self):
        r = self.root
        style = ttk.Style()
        style.configure("Big.TButton", font=("Segoe UI", 14, "bold"), padding=10)
        ttk.Button(r, text="🔍  SCAN SHOP  (F8)", style="Big.TButton",
                   command=self.scan).pack(fill="x", padx=8, pady=(8, 4))

        bar = ttk.Frame(r)
        bar.pack(fill="x", padx=8)
        ttk.Button(bar, text="Clear list", command=self.clear).pack(side="left")
        self.only_profit = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="Only profitable", variable=self.only_profit,
                        command=self.refresh).pack(side="left", padx=8)
        ttk.Button(bar, text="Update prices", command=self.update_db).pack(side="right")

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

        self.status = ttk.Label(r, text="Ready. Open a player shop and press SCAN.",
                                relief="sunken", anchor="w")
        self.status.pack(fill="x", side="bottom")

    def say(self, text):
        self.root.after(0, lambda: self.status.config(text=text))

    def start_hotkey(self):
        try:
            from pynput import keyboard
            keyboard.GlobalHotKeys({"<f8>": lambda: self.root.after(0, self.scan)}).start()
        except Exception:
            pass

    # ---- prices
    def update_db(self):
        if self.busy:
            return
        self.busy = True

        def work():
            try:
                found, total = update_prices(self.say)
                self.prices = load_prices()
                self.names = list(self.prices)
                if found == 0:
                    self.say("Could not read prices from MeowDB. Send update_log.txt (in %LOCALAPPDATA%\\MapleFlipper) to Claude.")
                else:
                    self.say(f"Prices updated: {total} items.")
            except Exception as e:
                self.say(f"Price update failed: {e}")
            self.busy = False

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
                for name, qty, price in items:
                    real, score = match_name(name, self.names)
                    self.seen[(real or name, price)] = (real, name, qty, price)
                self.root.after(0, self.refresh)
                n_good = sum(1 for v in self.seen.values() if self.profit(v) and self.profit(v) > 0)
                self.say(f"Found {len(items)} items on screen. Profitable so far: {n_good}. "
                         f"Scroll the shop and scan again for more.")
            except Exception as e:
                self.say(f"Scan failed: {e}")
            self.busy = False

        threading.Thread(target=work, daemon=True).start()

    def profit(self, v):
        real, _, qty, price = v
        if not real:
            return None
        return self.prices[real] * qty - price

    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        rows = sorted(self.seen.values(), key=lambda v: -(self.profit(v) or -1e12))
        for v in rows:
            real, raw, qty, price = v
            p = self.profit(v)
            if self.only_profit.get() and (p is None or p <= 0):
                continue
            if p is None:
                vals, tag = (f"{raw} (?)", qty, f"{price:,}", "unknown", "-"), "unknown"
            else:
                vals = (real, qty, f"{price:,}", f"{self.prices[real] * qty:,}", f"{p:+,}")
                tag = "good" if p > 0 else "bad"
            self.tree.insert("", "end", values=vals, tags=(tag,))

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

    def read_mesos(self):
        area = self.cfg.get("meso_area")
        if not area:
            return None
        nums = [to_int(o["text"]) for o in ocr(grab(area))]
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
        pending, hits = None, 0
        while self.tracking:
            try:
                v = self.read_mesos()
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
            time.sleep(0.7)

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
    App().run()
