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

VERSION = "0.40"
HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(os.environ.get("LOCALAPPDATA", HERE), "MapleFlipper")
os.makedirs(DATA, exist_ok=True)
PRICES_FILE = os.path.join(DATA, "prices.json")
CUSTOM_FILE = os.path.join(DATA, "custom_prices.txt")
CONFIG_FILE = os.path.join(DATA, "config.json")
LOG_FILE = os.path.join(DATA, "mesos_log.csv")
LEARN_FILE = os.path.join(DATA, "learned.json")
STATS_FILE = os.path.join(DATA, "flips.json")
REPO_ZIP = "https://github.com/Mpixy/MapleFlipper/archive/refs/heads/main.zip"
REPO_SOURCE = "https://raw.githubusercontent.com/Mpixy/MapleFlipper/main/maple_flipper.py"
BUNDLED_PRICES = os.path.join(getattr(sys, "_MEIPASS", HERE), "prices.json")
ITEM_URL = "https://meowdb.com/msclassic/item-db/{}"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"}

PRICE_LINE = re.compile(r"^\s*(\d[\d,.]*)\s*f[o0]r\s*(\d[\d,.]*)\s*m", re.I)


GROUPED = re.compile(r"^\d{1,3}([,.]\d{3})+$|^\d{1,4}$")  # "2,999" / "299": commas every 3 digits


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


def update_prices(progress, max_id=20000, path=PRICES_FILE):
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
                    if res != "error":  # blocked/slow requests don't mean we reached the end
                        misses += 1
            if stats.get("error", 0) > 20 * (1 + start // 1000):
                time.sleep(20)  # the site is pushing back: slow down
            done = start + 49
            left = (time.time() - t0) / max(done - first + 1, 1) * (max_id - done)
            print(f"[crawl] {done} found={found} misses={misses} {stats}", flush=True) if not hasattr(progress, "__self__") else None
            progress(f"Downloading prices... {done * 100 // max_id}%  ({done:,}/{max_id:,} checked, "
                     f"{found:,} items found)  ~{int(left // 60)}:{int(left % 60):02d} left at most", done / max_id)
            if found and misses > 4000:
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
    db = {**load_json(PRICES_FILE, {}), **db}  # keep items only found by a local download
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


# ---------------------------------------------------------------- self-learning

class Learned:
    """Corrections the app learned from real NPC sales and from the user.

    prices:  item -> real NPC price per unit (beats MeowDB)
    aliases: text read on screen -> the item it really is
    blocked: text read on screen -> items it must never be matched to
    """

    def __init__(self):
        d = load_json(LEARN_FILE, {})
        self.prices = d.get("prices", {})
        self.aliases = d.get("aliases", {})
        self.blocked = d.get("blocked", {})

    def save(self):
        save_json(LEARN_FILE, {"prices": self.prices, "aliases": self.aliases,
                               "blocked": self.blocked})

    def block(self, seen_text, wrong_item):
        lst = self.blocked.setdefault(norm(seen_text), [])
        if wrong_item not in lst:
            lst.append(wrong_item)
        self.aliases.pop(norm(seen_text), None)
        self.save()

    def alias(self, seen_text, right_item):
        self.aliases[norm(seen_text)] = right_item
        self.save()

    def set_price(self, item, unit_price):
        self.prices[item] = unit_price
        self.save()

    def forget(self, item):
        self.prices.pop(item, None)
        self.aliases = {k: v for k, v in self.aliases.items() if v != item}
        self.save()


# ---------------------------------------------------------------- OCR

_ocr = None
_ocr_lock = threading.Lock()  # the reader is used from the scan and the auto-detect threads


def ocr(img):
    global _ocr
    with _ocr_lock:
        if _ocr is None:
            from rapidocr_onnxruntime import RapidOCR
            try:
                _ocr = RapidOCR(intra_op_num_threads=max(2, min(6, (os.cpu_count() or 4) - 1)),
                                inter_op_num_threads=1)
            except TypeError:
                _ocr = RapidOCR()
        res, _ = _ocr(img, use_cls=False)  # text is never upside down: skip that step
    out = []
    for box, text, score in res or []:
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        out.append({"text": text.strip(), "x": min(xs), "y": (min(ys) + max(ys)) / 2,
                    "h": max(ys) - min(ys), "x2": max(xs)})
    return out


_rec_cache = {}


def read_line(img):
    """Read one known text line (no text detection). Same pixels -> cached answer, so after
    scrolling only the rows that actually changed cost anything."""
    key = hash(img.tobytes())
    if key not in _rec_cache:
        if len(_rec_cache) > 2000:
            _rec_cache.clear()
        with _ocr_lock:
            res, _ = _ocr(img, use_det=False, use_cls=False)
        # depending on the version a row is [box, text, score] or [text, score]
        text = next((x for x in (res[0] if res and res[0] else []) if isinstance(x, str)), "")
        _rec_cache[key] = text.strip()
    return _rec_cache[key]


def shop_rows(lines):
    """Where each (name line, price line) pair sits, so later scans can skip detection."""
    rows = []
    for ln in lines:
        if not PRICE_LINE.search(ln["text"]):
            continue
        above = [o for o in lines if o is not ln and abs(o["x"] - ln["x"]) < 40
                 and 0 < ln["y"] - o["y"] < 2.5 * max(ln["h"], 12)]
        if above:
            nm = max(above, key=lambda o: o["y"])
            rows.append([(int(r["x"]) - 4, int(r["y"] - r["h"] / 2) - 3, int(r["h"]) + 6, r["text"])
                         for r in (nm, ln)])
    # rows are evenly spaced: put back rows that were hidden (cursor, tooltip) during this read
    rows.sort(key=lambda r: r[1][1])
    if len(rows) >= 2:
        pitch = float(np.median([b[1][1] - a[1][1] for a, b in zip(rows, rows[1:])]))
        filled = [rows[0]]
        for row in rows[1:]:
            gap = round((row[1][1] - filled[-1][1][1]) / pitch) if pitch > 10 else 1
            for k in range(1, gap):
                prev = filled[-1]
                filled.append([(x, y + int(pitch), h, None) for x, y, h, _ in prev])
            filled.append(row)
        rows = filled
    return rows


def read_price(crop, verify=True):
    """Read a 'N for X mesos' line, and with verify=True a second time enlarged. OCR sometimes
    drops a digit ('2,999' -> '299'); the two reads then disagree. Returns (qty, price, sure) or None."""
    import cv2  # comes with rapidocr
    reads = []
    ims = [crop]
    if verify:
        ims.append(cv2.resize(crop, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC))
    for im in ims:
        m = PRICE_LINE.search(read_line(np.ascontiguousarray(im)))
        if m and to_int(m.group(1)) and to_int(m.group(2)):
            raw = m.group(2).strip(",.")
            reads.append((to_int(m.group(1)), to_int(raw), bool(GROUPED.match(raw))))
    if not reads:
        return None
    if not verify or (len(reads) == 2 and reads[0][:2] == reads[1][:2]):
        return reads[0][0], reads[0][1], reads[0][2]
    # disagree (or only one read): prefer a well-formed number, then the longer one
    # (a lost digit is far more common than an invented one) - but don't trust it
    qty, price, _ = max(reads, key=lambda r: (r[2], r[1]))
    return qty, price, False


def ink_width(crop):
    """Pixel width of the dark text in a 'N for X mesos' line (the game font has digits and
    letters of about the same width, so the width tells how many characters there really are)."""
    g = crop[:, :, :3].mean(axis=2) if crop.ndim == 3 else crop
    xs = np.nonzero((g < np.median(g) - 70).any(axis=0))[0]
    if len(xs) < 2:
        return None
    end = xs[0]
    for x in xs[1:]:  # stop at the first big gap (scrollbar / window border further right)
        if x - end > 20:
            break
        end = x
    return end - xs[0]


def faded(crop):
    """Sold out / not for sale: the game greys the row out (darkest text ~185 instead of ~0)."""
    g = crop[:, :, :3].mean(axis=2) if crop.ndim == 3 else crop
    return np.percentile(g, 2) > 120


last_faded = 0  # greyed-out rows the last read_rows skipped
_char_px = []  # recent pixel widths per character of shop price lines that read cleanly


def width_fits(w, qty, price, ref):
    """Does '1 for 2,999 mesos' have the width the line really has on screen?
    A dropped digit ('2,999' read as '299') makes it ~1-2 characters too short."""
    if not w or not ref:
        return True
    return abs(w - ref * len(f"{qty} for {price:,} mesos")) <= 0.5 * ref


def read_rows(img, rows, width=320, seed=False, worth=None):
    """Read the shop rows at known places. Returns (name, qty, price, row rect, sure) per row.
    seed=True stores the text the full scan already read, so nothing is read twice.
    worth(name, qty, price) -> True: only those rows get the slower double-check of the price
    (rows that aren't worth buying don't need it - that is most of them)."""
    global last_faded
    found, last_faded = [], 0
    for (nx, ny, nh, ntext), (px, py, ph, ptext) in rows:
        if min(nx, ny, px, py) < 0 or ny + nh > img.shape[0] or py + ph > img.shape[0]:
            return []
        crops = [np.ascontiguousarray(img[y:y + h, x:x + width])
                 for x, y, h in ((px, py, ph), (nx, ny, nh))]
        if faded(crops[0]):
            last_faded += 1
            continue  # greyed out: can't be bought, don't even read it
        if seed and ptext is not None:
            _rec_cache[hash(crops[0].tobytes())] = ptext
            _rec_cache[hash(crops[1].tobytes())] = ntext
        got = read_price(crops[0], verify=worth is None)
        if not got:
            continue  # empty slot / tooltip in the way
        name = read_line(crops[1])
        if re.search(r"[A-Za-z]{2}", name):
            rect = (nx - 50, ny - 4, width + 54, py + ph + 4 - (ny - 4))
            found.append((name, got, crops[0], rect, ink_width(crops[0])))
    # how wide one character is: the median over this page and earlier pages
    here = [w / len(f"{q} for {p:,} mesos") for _, (q, p, sure), _, _, w in found if w and sure]
    ref = float(np.median(_char_px + here)) if len(_char_px + here) >= 3 else None
    items = []
    for name, (qty, cost, sure), crop, rect, w in found:
        fits = width_fits(w, qty, cost, ref)
        if worth and (not sure or not fits or worth(name, qty, cost)):
            qty, cost, sure = read_price(crop) or (qty, cost, sure)
            fits = width_fits(w, qty, cost, ref)
        items.append((name, qty, cost, rect, sure and fits))
        if w and sure and fits:
            _char_px.append(w / len(f"{qty} for {cost:,} mesos"))
    del _char_px[:-60]
    return items


def grab(region=None):
    with mss.mss() as s:
        mon = region or s.monitors[0]
        shot = np.array(s.grab(mon))[:, :, :3]
    # upscale small regions so the digits read reliably
    if region and shot.shape[0] < 80:
        shot = np.repeat(np.repeat(shot, 3, axis=0), 3, axis=1)
    return np.ascontiguousarray(shot)


def find_shop_button(img):
    """Find the player-shop window (RGB image) by its orange BUY ITEM / LEAVE STORE buttons under the
    lime SELLER INFO button. Returns the BUY ITEM centre in image pixels, or None."""
    a = img[::2, ::2].astype(np.int16)
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    orange = (r > 170) & (g > 90) & (g < 175) & (b < 115) & (r - b > 100)
    lime = (g > 150) & (r > 100) & (b < 170) & (g >= r - 5) & (g - b > 40)
    c = 6
    h, w = (orange.shape[0] // c) * c, (orange.shape[1] // c) * c
    def cells(m):
        return m[:h, :w].reshape(h // c, c, w // c, c).mean(axis=(1, 3))
    oc, lc = cells(orange), cells(lime)
    ys, xs = np.nonzero(oc > 0.35)
    for y, x in zip(ys, xs):
        # a wide orange bar: several orange cells in a row
        if x + 5 >= oc.shape[1] or (oc[y, x:x + 6] > 0.3).sum() < 4:
            continue
        below = oc[y + 2:y + 6, x:x + 6] if y + 6 < oc.shape[0] else None   # LEAVE STORE
        above = lc[max(y - 9, 0):max(y - 2, 0), x:x + 6]                       # SELLER INFO
        if below is not None and (below > 0.3).sum() >= 3 and (above > 0.3).sum() >= 3:
            return (x + 3) * c * 2, y * c * 2
    return None


def seller_name(img, btn):
    """Read the seller's name tag under the avatar (left of the buttons)."""
    x, y = int(btn[0]), int(btn[1])
    tag = img[max(y + 62, 0):y + 95, max(x - 330, 0):max(x - 110, 0)]
    if not tag.size:
        return ""
    if _ocr is None:
        ocr(np.zeros((32, 32, 3), np.uint8))  # load the reader
    return read_line(np.ascontiguousarray(tag))


def same_seller(a, b):
    return bool(a) and bool(b) and Levenshtein.distance(norm(a), norm(b)) <= 2


def changed(a, b, limit=0.01):
    """True if more than `limit` of the pixels differ. The game's animated hand cursor
    alone changes ~0.5%, so it doesn't count as the list changing."""
    if a is None or b is None or a.shape != b.shape:
        return True
    return np.count_nonzero(np.any(a != b, axis=2)) > limit * a.shape[0] * a.shape[1]


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
        sure = bool(GROUPED.match(m.group(2).strip(",.")))
        above = [o for o in lines if o is not ln and abs(o["x"] - ln["x"]) < 40
                 and 0 < ln["y"] - o["y"] < 2.5 * max(ln["h"], 12)
                 and not PRICE_LINE.search(o["text"])]
        if above:
            name = max(above, key=lambda o: o["y"])["text"]
            items.append((name, qty, price, sure))
    return items


NPC_LINE = re.compile(r"^[^\d]{0,4}(\d[\d,.]*)\s*mes", re.I)  # "(coin) 1,500 Mesos"


def find_npc_items(lines):
    """Rows of an NPC shop / sell list: item name with 'N Mesos' under it. The stack size
    is the small number on the icon to the left."""
    items = []
    for ln in lines:
        if PRICE_LINE.search(ln["text"]) or not NPC_LINE.search(ln["text"]):
            continue
        price = to_int(NPC_LINE.search(ln["text"]).group(1))
        above = [o for o in lines if o is not ln and abs(o["x"] - ln["x"]) < 60
                 and 0 < ln["y"] - o["y"] < 2.5 * max(ln["h"], 12)
                 and not NPC_LINE.search(o["text"]) and re.search(r"[A-Za-z]{2}", o["text"])]
        if price is None or not above:
            continue
        name_ln = max(above, key=lambda o: o["y"])
        qty = 1
        for o in lines:
            if (re.fullmatch(r"\d{1,4}", o["text"]) and o["x"] < name_ln["x"] - 5
                    and ln["x"] - o["x"] < 120 and name_ln["y"] - 10 <= o["y"] <= ln["y"] + 25):
                qty = int(o["text"])
        items.append((name_ln["text"], qty, price))
    return items


def sale_combos(expects, total, most=6):
    """Sets of items (indexes) whose expected NPC prices add up to `total` - selling several
    things before the mesos are read again shows up as one big gain."""
    order = sorted((i for i, e in enumerate(expects) if e), key=lambda i: -expects[i])
    found = []

    def walk(start, left, picked):
        if left == 0 and picked:
            found.append(tuple(sorted(picked)))
            return
        if len(picked) == most or len(found) > 40:
            return
        for k in range(start, len(order)):
            i = order[k]
            if expects[i] <= left:
                walk(k + 1, left - expects[i], picked + [i])
    walk(0, total, [])
    return found


def lost_digits(short, full):
    """True if `short` is `full` with 1-2 digits dropped (how OCR misreads prices)."""
    if not 1 <= len(full) - len(short) <= 2:
        return False
    it = iter(full)
    return all(c in it for c in short)


def norm(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


def npc_value(name, unit_price, qty):
    """Stars/bullets are one rechargeable item: the shop shows the ammo count, not a stack."""
    if any(k in norm(name) for k in ("throwingstar", "bullet")):
        return unit_price
    return unit_price * qty


def match_name(name, names, learned=None):
    """Exact item only. Allows 1 OCR typo (2 for long names); anything more is another item."""
    if learned:
        if norm(name) in learned.aliases:
            return learned.aliases[norm(name)], 100
        blocked = set(learned.blocked.get(norm(name), []))
        if blocked:
            names = [n for n in names if n not in blocked]
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

With 'Auto' on you don't need F8 at all: the app notices when you open
a player shop and scans it, and rescans when you scroll.

FLIPS
The tracker shows what you bought and still have to sell, and counts
your profit per flip (today and all time).

THE APP LEARNS
- With mesos tracking on, buy an item and sell it to an NPC: the app checks
  the real price. If it was wrong, it fixes it for next time (marked ✓).
- Open any NPC shop: the app reads the real sell prices shown there
  (and the Buy Back tab) and fixes its prices and names by itself.
- Right-click a row to say 'wrong item', pick the right one, or type the
  real NPC price. It remembers forever.

Green frames are drawn right on the game's shop rows with the profit
(turn off with 'Marks'). Every page of the shop you are in stays in the
list. When you move on, profitable items stay (Shop column shows '← seller')
until you buy them; the rest is dropped.

ONE SCREEN? USE MINI MODE
Press F9 (or the 'Mini' button): the big window turns into a small bar that
stays on top of the game and shows only what matters - what to buy, the
mesos and your profit. Drag it anywhere; it remembers the spot. F9 again or
double-click it for the full window. Right-click it for more.

Orange '(?)' = no price known for that item.
Double-click any row to look the item up on MeowDB.
Min profit = only highlight items that earn at least this much.
'Update prices' = refresh prices after a game patch."""


def real_key(v):
    return (v[0] or v[1], v[3])


class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title(f"MapleFlipper v{VERSION}")
        self.root.attributes("-topmost", True)
        self.root.geometry("600x760")
        self.root.minsize(520, 600)
        self.cfg = load_json(CONFIG_FILE, {})
        if self.cfg.get("geometry"):
            self.root.geometry(self.cfg["geometry"])
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.report_callback_exception = lambda *exc: log_error(*exc)
        self.learned = Learned()
        self.stats = load_json(STATS_FILE, {"flips": [], "to_sell": []})
        # bought from player shops, waiting to be sold: [item, text read, qty, expected NPC value, paid]
        self.bought = self.stats["to_sell"]
        self.shop_box = None
        self.scanned = None
        self.rows = []
        self.incomplete = False
        self.shop_btn = None
        self.shop_sig = None
        self.shop_seen = 0.0
        self.busy_since = 0.0
        self.prices = load_prices()
        self.names = sorted(set(self.prices) | set(self.learned.prices))
        self.seen = {}
        self.seen_shop = {}  # item key -> seller name
        self.unsure = set()  # item keys whose price was read differently twice: never 'profitable'
        self.bought_keys = {}  # item keys you bought (mesos tracker) -> mesos paid
        self.cur_shop = ""
        self.tracking = False
        self.mesos = self.start_mesos = None
        self.busy = self.updating = False
        self.mini = None
        self.mini_on = False
        self.build_ui()
        self.show_stats()
        if self.cfg.get("mini"):
            self.root.after(200, self.toggle_mini)
        self.root.bind_all("<F8>", lambda e: self.scan())
        self.start_hotkey()
        threading.Thread(target=self.auto_loop, daemon=True).start()
        if len(self.prices) < 500 or os.path.exists(PRICES_FILE + ".progress"):  # first run / cut off
            self.root.after(300, self.update_db)
        else:
            self.say(f"Ready - {len(self.prices):,} item prices loaded. Open a player shop and press F8.")
        if not getattr(sys, "frozen", False) and not os.path.isdir(os.path.join(HERE, ".git")):
            self.root.after(3000, self.check_update)
        if self.cfg.get("tracking") and self.cfg.get("meso_area"):
            self.root.after(800, self.toggle_track)  # was tracking last time: keep going
        if not self.cfg.get("seen_help"):
            self.cfg["seen_help"] = True
            save_json(CONFIG_FILE, self.cfg)
            self.root.after(500, self.show_help)

    # ---- UI
    def build_ui(self):
        r = self.root
        logbox = ttk.LabelFrame(r, text="Activity")
        logbox.pack(fill="x", side="bottom", padx=8, pady=(0, 6))
        self.status = ttk.Label(logbox, text="Starting...", anchor="w", padding=3,
                                font=("Segoe UI", 10, "bold"), foreground="#1a5fb4")
        self.status.pack(fill="x")
        self.pbar = ttk.Progressbar(logbox, maximum=1.0)
        self.log = tk.Listbox(logbox, height=4, font=("Consolas", 9), activestyle="none")
        self.log.pack(fill="x", padx=4, pady=(0, 4))
        style = ttk.Style()
        style.configure("Big.TButton", font=("Segoe UI", 14, "bold"), padding=10)
        top = ttk.Frame(r)
        top.pack(fill="x", padx=8, pady=(4, 0))
        ttk.Label(top, text=f"v{VERSION}", font=("Segoe UI", 9, "bold"), foreground="#666").pack(side="left")
        self.update_btn = ttk.Button(top, text="", command=self.self_update)  # shown when there is one
        ttk.Button(r, text="🔍  SCAN THIS PAGE  (F8)", style="Big.TButton",
                   command=self.scan).pack(fill="x", padx=8, pady=(8, 4))

        self.banner = tk.Label(r, text="Open a player shop - I'll find the deals.", font=("Segoe UI", 15, "bold"),
                               bg="#e8e8e8", fg="#444", pady=8, wraplength=560)
        self.banner.pack(fill="x", padx=8, pady=(0, 6))
        self.shops_lbl = ttk.Label(r, foreground="#555", font=("Segoe UI", 9))
        self.shops_lbl.pack(anchor="w", padx=10)
        self.shops_today = 0
        self.found_today = 0

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
        self.show_marks = tk.BooleanVar(value=self.cfg.get("marks", True))
        ttk.Checkbutton(bar, text="Marks", variable=self.show_marks,
                        command=lambda: self.show_marks.get() or self.hide_marks()).pack(side="left", padx=(0, 6))
        self.auto = tk.BooleanVar(value=self.cfg.get("auto", True))
        ttk.Checkbutton(bar, text="Auto", variable=self.auto).pack(side="left", padx=(0, 6))
        self.beep = tk.BooleanVar(value=self.cfg.get("beep", True))
        ttk.Checkbutton(bar, text="Beep", variable=self.beep).pack(side="left")
        ttk.Button(bar, text="Update prices", command=self.update_db).pack(side="right")
        ttk.Button(bar, text="? Help", command=self.show_help).pack(side="right", padx=6)
        ttk.Button(bar, text="▭ Mini (F9)", command=self.toggle_mini).pack(side="right")

        cols = ("item", "qty", "price", "npc", "profit", "shop")
        self.tree = ttk.Treeview(r, columns=cols, show="headings", height=14)
        for c, w, t in zip(cols, (200, 40, 80, 80, 80, 90),
                           ("Item", "Qty", "Shop price", "NPC pays", "Profit", "Shop")):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="w" if c == "item" else "e")
        self.tree.tag_configure("good", background="#b8f5b0")
        self.tree.tag_configure("bad", foreground="#999")
        self.tree.tag_configure("unknown", foreground="#c07000")
        self.tree.tag_configure("unsure", background="#ffe7a8")
        self.tree.tag_configure("bought", background="#9cc8ff", font=("Segoe UI", 9, "bold"))
        self.tree.pack(fill="both", expand=True, padx=8, pady=6)
        self.tree.bind("<Double-1>", self.open_item)
        self.tree.bind("<Button-3>", self.row_menu)

        box = ttk.LabelFrame(r, text="Mesos tracker")
        box.pack(fill="x", padx=8, pady=4)
        row = ttk.Frame(box)
        row.pack(fill="x", padx=4, pady=4)
        ttk.Button(row, text="Set mesos area", command=self.set_area).pack(side="left")
        self.track_btn = ttk.Button(row, text="▶ Start tracking", command=self.toggle_track)
        self.track_btn.pack(side="left", padx=6)
        self.meso_lbl = ttk.Label(box, font=("Segoe UI", 12, "bold"), text="Mesos: -")
        self.meso_lbl.pack(anchor="w", padx=6)
        self.tosell_lbl = ttk.Label(box, foreground="#b35c00", wraplength=520, cursor="hand2")
        self.tosell_lbl.pack(anchor="w", padx=6)
        self.tosell_lbl.bind("<Button-1>", lambda e: self.sell_dialog())
        self.stats_lbl = ttk.Label(box, foreground="#555")
        self.stats_lbl.pack(anchor="w", padx=6)
        self.hist = tk.Listbox(box, height=4, font=("Consolas", 10))
        self.hist.pack(fill="x", padx=4, pady=4)



    def say(self, text, pct=None):
        def ui():
            self.status.config(text=text)
            if self.mini:
                self.mini_status.config(text=text)
            if pct is None:
                self.pbar.pack_forget()
            else:
                self.pbar.pack(fill="x", padx=4, pady=2, after=self.status)
                self.pbar["value"] = pct
            line = time.strftime("%H:%M:%S  ") + text
            # progress messages update in place instead of flooding the log
            if self.log.size() and pct is not None and self.log.get(0)[10:30] == line[10:30]:
                self.log.delete(0)
            self.log.insert(0, line)
            self.log.delete(200, "end")
        self.root.after(0, ui)

    def check_update(self):
        """Look for a newer version on GitHub every 20 minutes."""
        def work():
            try:
                src = requests.get(REPO_SOURCE, timeout=15).text
                m = re.search(r'VERSION = "([\d.]+)"', src)
                newer = m and tuple(map(int, m.group(1).split("."))) > tuple(map(int, VERSION.split(".")))
                if newer:
                    self.root.after(0, lambda: (self.update_btn.config(text=f"⬆ Update to v{m.group(1)}"),
                                                self.update_btn.pack(side="left", padx=8)))
            except Exception:
                pass
        threading.Thread(target=work, daemon=True).start()
        self.root.after(20 * 60 * 1000, self.check_update)

    def self_update(self):
        """Download the latest version over this one and restart."""
        import io
        import zipfile
        self.say("Updating...")
        try:
            z = zipfile.ZipFile(io.BytesIO(requests.get(REPO_ZIP, timeout=60).content))
            for info in z.infolist():
                rel = info.filename.split("/", 1)[1] if "/" in info.filename else ""
                if rel and not info.is_dir():
                    dest = os.path.join(HERE, rel)
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    with open(dest, "wb") as f:
                        f.write(z.read(info))
        except Exception as e:
            log_error(type(e), e, e.__traceback__)
            self.say(f"Update failed: {e}")
            return
        import subprocess
        subprocess.Popen([sys.executable, os.path.join(HERE, "maple_flipper.py")])
        self.on_close()

    def show_help(self):
        messagebox.showinfo("How to use MapleFlipper", HELP_TEXT)

    def auto_loop(self):
        """Finds a player shop by itself (orange BUY ITEM button), rescans it when the list
        changes (scrolling) and notices when it closes."""
        last, tried, checked, misses = None, None, 0.0, 0
        while True:
            time.sleep(0.12)
            if self.busy and time.time() - self.busy_since > 15:
                self.busy = False  # safety net: a scan never takes this long
            if not self.auto.get() or self.busy:
                continue
            try:
                with mss.mss() as sct:
                    mon = sct.monitors[0]
                box = self.shop_box
                if box and time.time() - checked > 0.5:  # is the shop still open / still there?
                    checked = time.time()
                    btn = find_shop_button(grab()[:, :, ::-1])  # screen grabs are BGR
                    moved = btn and self.shop_btn and (abs(btn[0] - self.shop_btn[0]) > 40
                                                       or abs(btn[1] - self.shop_btn[1]) > 40)
                    misses = misses + 1 if (not btn or moved) else 0
                    if misses >= 2:  # mouse hover can hide the button for a moment
                        misses = 0
                        self.shop_box, last, tried = None, None, None
                        self.root.after(0, self.hide_marks)
                        self.say("Shop closed - waiting for the next shop.")
                        continue
                if not box:  # no shop known: look for one (cheap colour check, no OCR)
                    time.sleep(0.25)
                    screen = grab()
                    btn = find_shop_button(screen[:, :, ::-1])
                    if not btn:
                        tried = None
                        continue
                    tried = tried or []
                    if any(abs(btn[0] - t[0]) < 40 and abs(btn[1] - t[1]) < 40 for t in tried):
                        continue  # already looked here (NPC shop / not a shop)
                    tried.append(btn)
                    sig = seller_name(screen, btn)
                    same_shop = same_seller(sig, self.shop_sig) and time.time() - self.shop_seen < 60
                    self.shop_btn, self.shop_sig = btn, sig
                    x, y = int(btn[0]) + mon["left"], int(btn[1]) + mon["top"]
                    cand = {"left": max(x - 400, mon["left"]), "top": max(y + 40, mon["top"]),
                            "width": 560, "height": 600}
                    if not same_shop:
                        self.root.after(0, self.new_shop, sig)
                    self.say(f"Shop of {sig or '?'} - reading it...")
                    self.root.after(0, self.scan, True, cand)
                    last = None
                    continue
                tried = None
                self.shop_seen = time.time()
                img = grab(box)
            except Exception:
                continue
            # the list stopped moving and shows something we haven't read yet -> scan it
            if not changed(img, last, 0.01) and changed(img, self.scanned, 0 if self.incomplete else 0.02):
                screen = grab()
                btn = find_shop_button(screen[:, :, ::-1])
                if not btn:
                    last = img
                    continue  # buttons hidden for a moment (mouse hover): try again
                if (abs(btn[0] - self.shop_btn[0]) > 40 or abs(btn[1] - self.shop_btn[1]) > 40
                        or not same_seller(seller_name(screen, btn), self.shop_sig)):
                    # another shop: forget this one so it is detected (and cleared) as new
                    self.shop_box, self.shop_btn, self.shop_sig, last, tried = None, None, None, None, None
                    self.root.after(0, self.hide_marks)
                    continue
                self.root.after(0, self.scan, True)
            last = img

    def start_hotkey(self):
        """Poll the real key state: works even when the game swallows the key press."""
        def loop():
            user32 = ctypes.windll.user32
            was_down = was9 = False
            while True:
                down = bool(user32.GetAsyncKeyState(0x77) & 0x8000)  # VK_F8
                if down and not was_down:
                    self.root.after(0, self.scan)
                was_down = down
                d9 = bool(user32.GetAsyncKeyState(0x78) & 0x8000)  # VK_F9: mini / full window
                if d9 and not was9:
                    self.root.after(0, self.toggle_mini)
                was9 = d9
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
    def visible_win(self):
        return self.mini if self.mini_on else self.root

    def overlaps_me(self, box):
        r = self.visible_win()
        x, y, w, h = r.winfo_rootx(), r.winfo_rooty(), r.winfo_width(), r.winfo_height()
        return not (x > box["left"] + box["width"] or x + w < box["left"]
                    or y > box["top"] + box["height"] or y + h < box["top"])

    def scan(self, auto=False, box=None):
        if self.busy:
            if not auto:
                self.say("Still working on the previous scan - one moment...")
            return
        self.busy, self.busy_since = True, time.time()
        if not auto:
            self.say("F8 pressed - reading the screen...")
        box = box or self.shop_box
        with mss.mss() as sct:
            full = sct.monitors[0]
        hide = self.overlaps_me(box or full)
        if hide:
            self.visible_win().attributes("-alpha", 0.0)  # don't OCR our own window
            self.root.update()
            time.sleep(0.1)
        try:
            img = grab(box) if box else grab()
            self.scanned = img if box else None
        except Exception as e:  # never leave the app stuck in "busy"
            log_error(type(e), e, e.__traceback__)
            self.shop_box, self.busy = None, False
            return
        finally:
            if hide:
                self.visible_win().attributes("-alpha", self.mini_alpha() if self.mini_on else 1.0)
        origin = box or full

        def work():
            try:
                if _ocr is None:
                    self.say("Loading the text reader (first scan only, a few seconds)...")
                t = time.time()
                lines, items, rects, src, mark_box = [], [], [], img, box
                all_grey = False
                if box and box is self.shop_box and self.rows:
                    items = read_rows(img, self.rows, worth=self.worth)  # fast: known rows, cached text
                    all_grey = not items and last_faded > 0
                if not items and not all_grey:  # (a page of only greyed-out rows: done)
                    lines = ocr(img)
                    items = find_shop_items(lines)
                    if box and box is self.shop_box and items:
                        self.rows = shop_rows(lines)
                        got = read_rows(img, self.rows, seed=True, worth=self.worth)
                        items = got if got or last_faded else items
                rects = [it[3] for it in items if len(it) == 5]
                items = [(it[0], it[1], it[2], it[-1]) for it in items]
                img_origin = origin
                if not items and not all_grey and (not self.shop_box or not auto):
                    # not a player shop here: read the whole screen (shop moved, or an NPC shop)
                    src = grab()
                    lines, img_origin = ocr(src), full
                    items = find_shop_items(lines)
                    if not items:
                        npc = find_npc_items(lines)
                        if npc:
                            self.root.after(0, self.learn_npc, npc)
                prices_l = [o for o in lines if PRICE_LINE.search(o["text"])]
                if items and prices_l and not (box and box is self.shop_box):  # remember where the shop is so next scans are fast
                    x0 = min(o["x"] for o in prices_l) + img_origin["left"]
                    y0 = min(o["y"] for o in prices_l) + img_origin["top"]
                    y1 = max(o["y"] for o in prices_l) + img_origin["top"]
                    # only the text columns: the map behind/next to the window keeps moving
                    nb = {"left": int(max(x0 - 12, full["left"])), "top": int(max(y0 - 55, full["top"])),
                          "width": 340, "height": int(y1 - y0 + 75)}
                    # reuse this scan for the list area: row positions + text, no second scan
                    dx, dy = img_origin["left"] - nb["left"], img_origin["top"] - nb["top"]
                    sx, sy = -dx, -dy
                    sub = src[sy:sy + nb["height"], sx:sx + nb["width"]] if sx >= 0 and sy >= 0 else None
                    self.rows = []
                    if sub is not None and sub.shape[:2] == (nb["height"], nb["width"]):
                        sub = np.ascontiguousarray(sub)
                        shifted = [{**o, "x": o["x"] + dx, "y": o["y"] + dy} for o in lines]
                        self.rows = shop_rows(shifted)
                        got = read_rows(sub, self.rows, seed=True, worth=self.worth)
                        if got:
                            items = [(it[0], it[1], it[2], it[4]) for it in got]
                            rects = [it[3] for it in got]
                            self.scanned = sub  # the loop now only rescans when the list changes
                        elif last_faded:
                            items, rects = [], []  # everything on this page is greyed out
                            self.scanned = sub
                        else:
                            self.rows = []
                    self.shop_box = nb
                    mark_box = nb
                elif box and not items and not auto:
                    self.shop_box = None
                # a row hidden (mouse cursor, tooltip): re-read as soon as anything moves
                self.incomplete = bool(self.rows) and len(items) + last_faded < len(self.rows)
                # the list itself is only touched on the window's thread (no races with it)
                self.root.after(0, self.apply_scan, items, rects, mark_box, auto, time.time() - t)
            except Exception as e:
                log_error(type(e), e, e.__traceback__)
                self.say(f"Scan failed: {e}")
            self.busy = False

        threading.Thread(target=work, daemon=True).start()

    def worth(self, name, qty, price):
        """Would this row show up as profitable? (only those get their price double-checked)"""
        real, _ = match_name(name, self.names, self.learned)
        pays = self.npc_pays(real, qty) if real else None
        return pays is None or pays - price > 0

    def unit_price(self, item):
        return self.learned.prices.get(item, self.prices.get(item))

    def npc_pays(self, item, qty):
        u = self.unit_price(item)
        return None if u is None else npc_value(item, u, qty)

    def apply_scan(self, items, rects, mark_box, auto, took):
        new_good, marks = False, []
        for i, (name, qty, price, sure) in enumerate(items):
            real, score = match_name(name, self.names, self.learned)
            key = (real or name, price)
            if not real:  # unknown item: a one-letter misread of a row we already have
                key = next((k for k in self.seen if k[1] == price and not self.seen[k][0]
                            and Levenshtein.distance(norm(k[0]), norm(name)) <= 2), key)
            v = (real, name, qty, price)
            if sure:
                self.unsure.discard(key)
            else:
                self.unsure.add(key)
            p = self.profit(v)
            good = sure and p is not None and p > 0 and p >= self.min_p()
            if key not in self.seen and good:
                new_good = True
            self.seen[key] = v
            self.seen_shop[key] = self.cur_shop
            if rects and good:
                marks.append((rects[i], p))
        if mark_box and mark_box is self.shop_box:
            self.draw_marks(mark_box, marks)
        best = max(marks, key=lambda m: m[1])[1] if marks else 0
        if new_good:
            self.found_today += sum(p for _, p in marks)
        if new_good and self.beep.get():
            import winsound
            if best >= max(10000, 10 * self.min_p()):  # jackpot: a little fanfare
                threading.Thread(target=lambda: [winsound.Beep(f, 90) for f in (660, 880, 1320)],
                                 daemon=True).start()
            else:
                winsound.MessageBeep(winsound.MB_ICONASTERISK)
        self.show_banner()
        self.refresh()
        n_good = sum(1 for k, v in self.seen.items() if (self.profit(v) or 0) > 0
                     and k not in self.unsure and k not in self.bought_keys)
        if items or not auto:
            self.say(("Page changed - " if auto else "") + f"Scan took {took:.1f}s. Found {len(items)} items. "
                     f"Profitable so far: {n_good}.")

    def set_banner(self, text, bg, fg):
        self.banner.config(text=text, bg=bg, fg=fg)
        if self.mini:
            self.mini_deal.config(text=text, bg=bg, fg=fg)

    def show_banner(self):
        """One big line: what to buy in this shop right now (or: move on)."""
        here = [(self.profit(v), v) for k, v in self.seen.items()
                if self.seen_shop.get(k) == self.cur_shop and k not in self.unsure and k not in self.bought_keys]
        here = [(p, v) for p, v in here if p is not None and p > 0 and p >= self.min_p()]
        if here:
            p, v = max(here, key=lambda x: x[0])
            more = f"   (+{len(here) - 1} more, total +{sum(x[0] for x in here):,})" if len(here) > 1 else ""
            hot = p >= max(10000, 10 * self.min_p())
            self.set_banner(f"{'💰' if hot else '✅'} BUY: {v[0]}  for {v[3]:,}  →  +{p:,}{more}",
                            "#ffd23f" if hot else "#2fb344", "#000" if hot else "#fff")
        elif self.cur_shop:
            self.set_banner(f"✖ Nothing here{' at ' + self.cur_shop if self.cur_shop else ''} - next shop!",
                            "#e8e8e8", "#777")
        self.shops_lbl.config(text=f"Shops checked this session: {self.shops_today}    "
                                   f"Profit spotted: +{self.found_today:,}")

    def profit(self, v):
        real, _, qty, price = v
        pays = self.npc_pays(real, qty) if real else None
        return None if pays is None else pays - price

    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        rows = sorted(self.seen.values(), key=lambda v: -(self.profit(v) or -1e12))
        rows.sort(key=lambda v: (real_key(v) not in self.bought_keys))  # bought rows on top
        for v in rows:
            real, raw, qty, price = v
            key = real_key(v)
            p = self.profit(v)
            bought, unsure = key in self.bought_keys, key in self.unsure
            if self.only_profit.get() and not bought and not unsure and (p is None or p <= 0 or p < self.min_p()):
                continue
            shop = self.seen_shop.get(key, "")
            if shop and shop != self.cur_shop:
                shop = "← " + shop  # from a shop you already left
            if bought:
                pays = self.npc_pays(real, qty)
                res = "?" if pays is None else f"{pays - price:+,}"
                vals, tag = (f"✔ BOUGHT  {real}", qty, f"{price:,}", "-" if pays is None else f"{pays:,}",
                             res, "sell to NPC"), "bought"
            elif unsure:
                pays = self.npc_pays(real, qty) if real else None
                vals, tag = (f"{real or raw} (price?)", qty, f"{price:,} ?", "-" if pays is None else f"{pays:,}",
                             "check price", shop), "unsure"
            elif p is None:
                vals, tag = (f"{raw} (?)", qty, f"{price:,}", "unknown", "-", shop), "unknown"
            else:
                mark = " ✓" if real in self.learned.prices else ""
                vals = (real, qty, f"{price:,}", f"{self.npc_pays(real, qty):,}{mark}", f"{p:+,}", shop)
                tag = "good" if p > 0 and p >= self.min_p() else "bad"
            self.tree.insert("", "end", iid=f"{raw}|{price}", values=vals, tags=(tag,))

    # ---- teaching
    def row_value(self, iid):
        raw, price = iid.rsplit("|", 1)
        for v in self.seen.values():
            if v[1] == raw and v[3] == int(price):
                return v
        return None

    def row_menu(self, event):
        iid = self.tree.identify_row(event.y)
        v = iid and self.row_value(iid)
        if not v:
            return
        self.tree.selection_set(iid)
        real, raw, qty, price = v
        m = tk.Menu(self.root, tearoff=0)
        if real:
            m.add_command(label=f"Wrong item - it is NOT '{real}'", command=lambda: self.fix_item(v, wrong=True))
        m.add_command(label="Pick the correct item...", command=lambda: self.fix_item(v))
        if real:
            m.add_command(label=f"Set real NPC price of '{real}'...", command=lambda: self.ask_price(real))
            if real in self.learned.prices:
                m.add_command(label="Forget what was learned for this item",
                              command=lambda: (self.learned.forget(real), self.rematch()))
        m.tk_popup(event.x_root, event.y_root)

    def fix_item(self, v, wrong=False):
        real, raw, qty, price = v
        if wrong:
            self.learned.block(raw, real)
            self.say(f"Learned: '{raw}' is not '{real}'.")
        top = tk.Toplevel(self.root)
        top.title("Which item is it?")
        top.attributes("-topmost", True)
        ttk.Label(top, text=f"Shop shows: {raw}\nType to search, then double-click the right item:").pack(padx=8, pady=6)
        q = tk.StringVar(value=raw.rstrip(".… "))
        ent = ttk.Entry(top, textvariable=q, width=40)
        ent.pack(padx=8)
        lb = tk.Listbox(top, height=12, width=45)
        lb.pack(padx=8, pady=6)
        blocked = set(self.learned.blocked.get(norm(raw), []))

        def fill(*_):
            key = norm(q.get())
            hits = sorted((n for n in self.names if key in norm(n) and n not in blocked),
                          key=lambda n: (len(n), n))[:200]
            lb.delete(0, "end")
            for n in hits:
                lb.insert("end", n)

        def choose(_=None):
            if lb.curselection():
                item = lb.get(lb.curselection()[0])
                self.learned.alias(raw, item)
                self.say(f"Learned: '{raw}' = '{item}'. It will be matched this way from now on.")
                top.destroy()
                self.rematch()

        q.trace_add("write", fill)
        lb.bind("<Double-1>", choose)
        ttk.Button(top, text="Use selected", command=choose).pack(pady=(0, 8))
        fill()
        ent.focus_set()
        if wrong:
            self.rematch()

    def ask_price(self, item):
        from tkinter import simpledialog
        cur = self.unit_price(item)
        prompt = f"How much does an NPC pay for ONE '{item}'?"
        if cur is not None:
            prompt += f"\n(currently {cur:,})"
        val = simpledialog.askinteger("Real NPC price", prompt, parent=self.root,
                                      initialvalue=cur, minvalue=0)
        if val is not None:
            self.learned.set_price(item, val)
            self.say(f"Learned: NPC pays {val:,} for '{item}'.")
            self.refresh()

    def rematch(self):
        """Re-run matching on everything in the list after the app learned something."""
        old = list(self.seen.values())
        self.seen.clear()
        for v in old:
            _, raw, qty, price = v
            real, _ = match_name(raw, self.names, self.learned)
            key = (real or raw, price)
            self.seen[key] = (real, raw, qty, price)
            if real_key(v) in self.bought_keys:
                self.bought_keys[key] = self.bought_keys.pop(real_key(v))
            if real_key(v) in self.unsure:
                self.unsure.discard(real_key(v))
                self.unsure.add(key)
        self.refresh()

    def learn_from_mesos(self, diff):
        """Buying from a shop and selling to an NPC show up as mesos changes: use them to check
        and correct the prices automatically."""
        if diff < 0:
            spent = -diff
            cands = [(k, v) for k, v in self.seen.items() if v[0] and k not in self.bought_keys]
            hit = [kv for kv in cands if kv[1][3] == spent]
            if not hit:  # the shop price was misread (a digit lost: 2,999 read as 299)?
                hit = [kv for kv in cands if lost_digits(str(kv[1][3]), str(spent))]
            if len(hit) != 1 and not any(kv[1][3] == spent for kv in hit):
                return
            key, v = next((kv for kv in hit if kv[1][3] == spent), hit[0])
            real, raw, qty, price = v
            if price != spent:  # show what you really paid
                self.seen.pop(key, None)
                self.unsure.discard(key)
                v = (real, raw, qty, spent)
                key = real_key(v)
                self.seen[key] = v
                self.seen_shop.setdefault(key, self.cur_shop)
            self.bought_keys[key] = spent
            pays = self.npc_pays(real, qty)
            self.bought.append([real, raw, qty, pays, spent])
            self.save_stats()
            self.root.after(0, self.refresh)
            self.root.after(0, self.show_banner)
            res = "" if pays is None else f" NPC pays {pays:,} -> {pays - spent:+,}."
            fix = f" (the shop read {price:,} - fixed)" if price != spent else ""
            self.say(f"✔ BOUGHT '{real}' for {spent:,}{fix}.{res} Now sell it to an NPC.")
            return
        if not self.bought:
            return
        for i, (item, _, qty, expect, paid) in enumerate(self.bought):
            if expect == diff:
                self.bought.pop(i)
                if item not in self.learned.prices:
                    self.learned.set_price(item, self.unit_price(item))
                self.record_flip(item, paid, diff)
                self.sold(item)
                self.say(f"✓ Confirmed: '{item}' sold for {diff:,} exactly as predicted.")
                self.refresh()
                return
        combos = sale_combos([b[3] for b in self.bought], diff)
        # the same names in a different order are the same answer (e.g. which of 3 Gold Earrings)
        kinds = {tuple(sorted(self.bought[i][0] for i in c)) for c in combos}
        if len(kinds) == 1:
            self.sold_many(combos[0])
        elif len(self.bought) == 1:
            self.apply_sale(0, diff)
        else:
            self.sell_dialog(diff, combos[0] if combos else ())

    def sold(self, item):
        """Sold to the NPC: the 'BOUGHT' row is done."""
        for k in [k for k in self.bought_keys if k[0] == item][:1]:
            self.bought_keys.pop(k)
            self.seen.pop(k, None)

    def save_stats(self):
        save_json(STATS_FILE, self.stats)
        self.root.after(0, self.show_stats)

    def record_flip(self, item, paid, got):
        self.stats["flips"].append({"time": time.strftime("%Y-%m-%d %H:%M"), "item": item,
                                    "paid": paid, "got": got})
        self.save_stats()

    def show_stats(self):
        today = time.strftime("%Y-%m-%d")
        flips = self.stats["flips"]
        t = [f for f in flips if f["time"].startswith(today)]
        prof = lambda fs: sum(f["got"] - f["paid"] for f in fs)
        self.stats_lbl.config(text=f"Flips today: {len(t)}  profit {prof(t):+,}    "
                                   f"All time: {len(flips)}  profit {prof(flips):+,}")
        if self.bought:
            todo = ", ".join(f"{b[0]} (+{b[3] - b[4]:,})" if b[3] is not None else b[0]
                             for b in self.bought)
            self.tosell_lbl.config(text=f"To sell to an NPC: {todo}   (click to mark sold)")
        else:
            self.tosell_lbl.config(text="To sell to an NPC: nothing")
        self.update_mini_mesos()

    def learn_npc(self, rows):
        """An NPC shop shows the real price for everything you could sell (and the Buy Back
        tab shows what you just sold): treat it as ground truth."""
        learned, fixed = 0, []
        for name, qty, price in rows:
            if name.rstrip().endswith((".", "…")) or len(norm(name)) < 4:
                continue
            real, score = match_name(name, self.names, self.learned)
            if not real or score < 100:
                if " " not in name.strip():
                    continue  # OCR glued the words together: not reliable enough to learn
                real = name.strip()  # not in MeowDB: remember it as shown
            known = self.unit_price(real)
            if qty <= 1 and known and price > known and price % known == 0:
                continue  # a stack (the count on the icon wasn't readable): price matches
            unit = price if (qty <= 1 or npc_value(real, 1, qty) == 1) else round(price / qty)
            # something bought from a player shop under a shorter / wrong name?
            for i, (b_real, b_raw, b_qty, expect, paid) in enumerate(self.bought):
                if b_real != real and norm(real).startswith(norm(b_raw.rstrip(".… "))):
                    self.bought.pop(i)
                    self.save_stats()
                    if norm(b_raw) in {norm(n) for n in self.prices}:
                        # the shorter name is a real item too: can't remap it, just warn
                        fixed.append(f"you bought '{real}', not '{b_raw}' (the shop name was cut off)")
                    else:
                        self.learned.alias(b_raw, real)
                        fixed.append(f"'{b_raw}' is really '{real}'")
                    break
            if self.unit_price(real) != unit or real not in self.learned.prices:
                self.learned.set_price(real, unit)
                learned += 1
        if real_names := set(self.learned.prices) - set(self.names):
            self.names += sorted(real_names)
        if fixed:
            self.say("Corrected: " + "; ".join(fixed))
        if learned:
            self.say(f"Learned {learned} real NPC prices from the NPC shop.")
        if learned or fixed:
            self.rematch()

    def apply_sale(self, i, got):
        item, _, qty, expect, paid = self.bought.pop(i)
        stars = npc_value(item, 1, qty) == 1
        unit = got if stars or qty == 1 else round(got / qty)
        self.learned.set_price(item, unit)
        self.record_flip(item, paid, got)
        self.sold(item)
        self.say(f"Corrected: '{item}' sold for {got:,}, not {expect:,}. Price fixed for the future.")
        self.refresh()

    def sold_many(self, idxs, quiet=False):
        """Several items sold together at their expected prices."""
        done = []
        for i in sorted(idxs, reverse=True):
            item, _, qty, expect, paid = self.bought.pop(i)
            self.record_flip(item, paid, expect if expect is not None else paid)
            self.sold(item)
            done.append(item)
        self.save_stats()
        if done and not quiet:
            self.say(f"✓ Sold {len(done)} items: {', '.join(reversed(done))}")
        self.refresh()
        self.show_banner()

    def drop_bought(self, idxs):
        """Not sold through the tracker (sold earlier, kept it, dropped it): just forget them."""
        for i in sorted(idxs, reverse=True):
            self.sold(self.bought.pop(i)[0])
        self.save_stats()
        self.refresh()
        self.show_banner()

    def sell_dialog(self, got=None, preset=()):
        """Tick what you sold. Opened after a sale the app couldn't match by itself, or by
        clicking the 'Sell to NPC' list."""
        if getattr(self, "sell_top", None) and self.sell_top.winfo_exists():
            self.sell_top.destroy()
        if not self.bought:
            return
        top = self.sell_top = tk.Toplevel(self.root)
        top.title("What did you sell?")
        top.attributes("-topmost", True)
        head = (f"You got {got:,} mesos. Tick everything you sold:" if got
                else "Tick what you already sold (or don't have any more):")
        ttk.Label(top, text=head, font=("Segoe UI", 10, "bold")).pack(padx=10, pady=(8, 4), anchor="w")
        total = ttk.Label(top, foreground="#555")
        marks = []
        lst = ttk.Frame(top)
        lst.pack(fill="both", padx=10)

        def recount(*_):
            t = sum(b[3] or 0 for b, v in zip(self.bought, marks) if v.get())
            ok = got and t == got
            total.config(text=f"Ticked: {t:,}" + (f"  /  got {got:,}" if got else "") + ("  ✓" if ok else ""),
                         foreground="#1a8f3a" if ok else "#555")
        for i, (item, _, qty, expect, paid) in enumerate(self.bought):
            v = tk.BooleanVar(value=i in preset)
            marks.append(v)
            gain = "" if expect is None else f"   {expect:,}  ({expect - paid:+,})"
            ttk.Checkbutton(lst, text=f"{item}{gain}", variable=v, command=recount).pack(anchor="w")
        total.pack(padx=10, pady=4, anchor="w")
        recount()
        picked = lambda: [i for i, v in enumerate(marks) if v.get()]

        def sold():
            idxs = picked()
            top.destroy()
            if got and len(idxs) == 1:
                self.apply_sale(idxs[0], got)  # one item: the real price is known now
            else:
                self.sold_many(idxs)

        def remove():
            idxs = picked()
            top.destroy()
            self.drop_bought(idxs)
        row = ttk.Frame(top)
        row.pack(fill="x", padx=10, pady=8)
        ttk.Button(row, text="✓ Sold the ticked ones", command=sold).pack(side="left")
        ttk.Button(row, text="Just remove them", command=remove).pack(side="left", padx=6)
        ttk.Button(row, text="Clear all", command=lambda: (top.destroy(), self.drop_bought(range(len(self.bought))))
                   ).pack(side="left")
        ttk.Button(row, text="Cancel", command=top.destroy).pack(side="right")

    # ---- green frames drawn right on the game's shop window
    def draw_marks(self, box, marks):
        if not self.show_marks.get() or not marks or box is not self.shop_box:
            self.hide_marks()
            return
        if not getattr(self, "overlay", None):
            ov = tk.Toplevel(self.root)
            ov.overrideredirect(True)
            ov.attributes("-topmost", True)
            ov.configure(bg="#ff00ff")
            try:
                ov.attributes("-transparentcolor", "#ff00ff")  # Windows: magenta = see-through
            except tk.TclError:
                ov.attributes("-alpha", 0.6)
            self.ov_canvas = tk.Canvas(ov, bg="#ff00ff", highlightthickness=0)
            self.ov_canvas.pack(fill="both", expand=True)
            ov.update_idletasks()
            try:  # let mouse clicks pass through to the game
                hwnd = ctypes.windll.user32.GetParent(ov.winfo_id())
                style = ctypes.windll.user32.GetWindowLongW(hwnd, -20)
                ctypes.windll.user32.SetWindowLongW(hwnd, -20, style | 0x80000 | 0x20)
            except Exception:
                pass
            self.overlay = ov
        pad = 70  # rows start left of the watched text area (item icons)
        w = box["width"] + pad + 140
        self.overlay.geometry(f"{w}x{box['height']}+{box['left'] - pad}+{box['top']}")
        self.overlay.deiconify()
        c = self.ov_canvas
        c.delete("all")
        best = max(p for _, p in marks)
        for (x, y, rw, rh), p in marks:
            x += pad
            top = p == best and len(marks) > 1
            color = "#ffb000" if top else "#00e000"
            c.create_rectangle(x, y, x + rw, y + rh, outline=color, width=4 if top else 3)
            c.create_text(x + rw + 6, y + rh / 2, anchor="w", text=f"{'★ ' if top else ''}+{p:,}",
                          fill="#e09000" if top else "#00c000", font=("Segoe UI", 12 if top else 11, "bold"))

    def hide_marks(self):
        if getattr(self, "overlay", None):
            self.overlay.withdraw()

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
        self.cfg.update(tracking=self.tracking, marks=self.show_marks.get(),
                        geometry=self.root.geometry(), min_profit=self.min_p(), beep=self.beep.get(),
                        auto=self.auto.get(), mini=self.mini_on)
        if self.mini:
            self.cfg["mini_pos"] = [self.mini.winfo_x(), self.mini.winfo_y()]
        save_json(CONFIG_FILE, self.cfg)
        self.root.destroy()

    def new_shop(self, seller):
        """Another shop: drop the losers of the previous ones, keep the profitable items
        until they are bought."""
        for key, v in list(self.seen.items()):
            p = self.profit(v)
            if (p is None or p <= 0 or p < self.min_p()) and key not in self.bought_keys:
                del self.seen[key]
                self.unsure.discard(key)
        self.cur_shop = seller
        self.shops_today += 1
        self.refresh()
        self.show_banner()

    def clear(self):
        self.seen = {k: v for k, v in self.seen.items() if k in self.bought_keys}
        self.unsure.clear()
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
        texts = [o["text"].replace(" ", "") for o in ocr(grab(area) if img is None else img)]
        # only things that look like a mesos amount ("253,404"): not stray digits around it
        nums = [to_int(t) for t in texts if GROUPED.match(t.strip(",."))]
        return max(nums) if nums else None

    def toggle_track(self):
        if not self.cfg.get("meso_area"):
            messagebox.showinfo("MapleFlipper", "First click 'Set mesos area' and drag a box "
                                "around the mesos number in your inventory window.")
            return
        self.tracking = not self.tracking
        self.track_btn.config(text="■ Stop tracking" if self.tracking else "▶ Start tracking")
        self.say("Mesos tracking started - watching your inventory." if self.tracking
                 else "Mesos tracking stopped.")
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
                if last_img is not None and pending == self.mesos and np.array_equal(img, last_img):
                    continue  # nothing changed on screen: skip the expensive OCR
                last_img = img
                v = self.read_mesos(img)
            except Exception:
                v = None
            if v is not None:
                # require identical reads in a row so OCR glitches aren't logged; a value that
                # looks like the old one with a digit lost/added has to hold much longer
                if v == pending:
                    hits += 1
                else:
                    pending, hits = v, 1
                old = self.mesos
                glitch = old is not None and (lost_digits(str(v), str(old)) or lost_digits(str(old), str(v)))
                if hits == (6 if glitch else 2) and v != old:
                    self.root.after(0, self.on_mesos, v)

    def on_mesos(self, v):
        old = self.mesos
        self.mesos = v
        if self.start_mesos is None:
            self.start_mesos = v
            self.say(f"Mesos read: {v:,} - this is the starting point.")
        elif old is not None:
            diff = v - old
            kind = "SOLD to NPC / gained" if diff > 0 else "Spent / bought"
            stamp = time.strftime("%H:%M:%S")
            self.hist.insert(0, f"{stamp}  {diff:+12,}  {kind}")
            self.learn_from_mesos(diff)  # says something only when it is a buy / sale it recognises
            new_file = not os.path.exists(LOG_FILE)
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                if new_file:
                    f.write("time,change,mesos\n")
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')},{diff},{v}\n")
        total = v - self.start_mesos
        self.meso_lbl.config(text=f"Mesos: {v:,}    Session profit: {total:+,}",
                             foreground="green" if total >= 0 else "red")
        self.update_mini_mesos()

    # ---- mini mode: a small bar over the game for one-screen players
    def mini_alpha(self):
        return self.cfg.get("mini_alpha", 0.92)

    def build_mini(self):
        m = tk.Toplevel(self.root)
        m.overrideredirect(True)
        m.attributes("-topmost", True)
        m.attributes("-alpha", self.mini_alpha())
        m.configure(bg="#1f1f24")
        top = tk.Frame(m, bg="#1f1f24")
        top.pack(fill="x")
        grip = tk.Label(top, text=" ⠿ ", bg="#1f1f24", fg="#888", cursor="fleur", font=("Segoe UI", 11))
        grip.pack(side="left")
        btn = dict(bg="#33333b", fg="#fff", bd=0, activebackground="#4a4a55", activeforeground="#fff",
                   font=("Segoe UI", 10, "bold"), padx=8, cursor="hand2")
        tk.Button(top, text="✕", command=self.on_close, **btn).pack(side="right", padx=(2, 4), pady=3)
        tk.Button(top, text="⤢", command=self.toggle_mini, **btn).pack(side="right", padx=2, pady=3)
        tk.Button(top, text="🔍 Scan", command=self.scan, **btn).pack(side="right", padx=2, pady=3)
        self.mini_mesos = tk.Label(top, text="Mesos: -", bg="#1f1f24", fg="#ddd", font=("Segoe UI", 10, "bold"))
        self.mini_mesos.pack(side="left", padx=4)
        self.mini_deal = tk.Label(m, text=self.banner.cget("text"), bg=self.banner.cget("bg"),
                                  fg=self.banner.cget("fg"), font=("Segoe UI", 12, "bold"),
                                  wraplength=400, justify="left", anchor="w", padx=8, pady=5)
        self.mini_deal.pack(fill="x")
        self.mini_status = tk.Label(m, text=self.status.cget("text"), bg="#1f1f24", fg="#9ab",
                                    font=("Segoe UI", 8), anchor="w", padx=6)
        self.mini_status.pack(fill="x", pady=(1, 3))
        self.mini_sell = tk.Label(m, text="", bg="#1f1f24", fg="#ffb35c", font=("Segoe UI", 9, "bold"),
                                  anchor="w", padx=6, wraplength=400, justify="left")

        def press(e):
            m._drag = (e.x_root - m.winfo_x(), e.y_root - m.winfo_y())

        def drag(e):
            dx, dy = m._drag
            m.geometry(f"+{e.x_root - dx}+{e.y_root - dy}")

        def menu(e):
            mm = tk.Menu(m, tearoff=0)
            mm.add_command(label="Full window (F9)", command=self.toggle_mini)
            mm.add_command(label="Scan (F8)", command=self.scan)
            mm.add_checkbutton(label="Auto-detect shops", variable=self.auto)
            mm.add_checkbutton(label="Beep", variable=self.beep)
            mm.add_checkbutton(label="Marks on the shop", variable=self.show_marks,
                               command=lambda: self.show_marks.get() or self.hide_marks())
            op = tk.Menu(mm, tearoff=0)
            for a in (1.0, 0.92, 0.8, 0.65, 0.5):
                op.add_command(label=f"{int(a * 100)}%", command=lambda a=a: self.set_mini_alpha(a))
            mm.add_cascade(label="See-through", menu=op)
            mm.add_command(label="Clear list", command=self.clear)
            mm.add_command(label="Mark items as sold...", command=self.sell_dialog)
            mm.add_separator()
            mm.add_command(label="Quit", command=self.on_close)
            mm.tk_popup(e.x_root, e.y_root)

        for w in (m, grip, top, self.mini_mesos, self.mini_deal, self.mini_status, self.mini_sell):
            w.bind("<Button-1>", press)
            w.bind("<B1-Motion>", drag)
            w.bind("<Double-1>", lambda e: self.toggle_mini())
            w.bind("<Button-3>", menu)
        self.mini_sell.config(cursor="hand2")
        self.mini_sell.bind("<Button-1>", lambda e: self.sell_dialog())
        self.mini_sell.unbind("<B1-Motion>")
        self.mini_sell.unbind("<Double-1>")
        pos = self.cfg.get("mini_pos")
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        if not pos or not (0 <= pos[0] < sw - 40 and 0 <= pos[1] < sh - 20):
            pos = [sw // 2 - 210, 4]  # top middle: above the game's own windows
        m.geometry(f"+{pos[0]}+{pos[1]}")
        m.minsize(420, 1)
        self.mini = m
        self.update_mini_mesos()

    def set_mini_alpha(self, a):
        self.cfg["mini_alpha"] = a
        if self.mini:
            self.mini.attributes("-alpha", a)

    def update_mini_mesos(self):
        if not self.mini:
            return
        if self.mesos is None:
            self.mini_mesos.config(text="Mesos: -", fg="#ddd")
        else:
            total = self.mesos - self.start_mesos
            self.mini_mesos.config(text=f"{self.mesos:,}   {total:+,}", fg="#5ee07a" if total >= 0 else "#ff6b6b")
        names = [b[0] for b in self.bought]
        if names:
            todo = ", ".join(names[:3]) + (f" +{len(names) - 3} more" if len(names) > 3 else "")
            self.mini_sell.config(text=f"➜ Sell to NPC: {todo}   (click = sold)")
            self.mini_sell.pack(fill="x", pady=(0, 3))
        else:
            self.mini_sell.pack_forget()

    def toggle_mini(self):
        if not self.mini:
            self.build_mini()
        self.mini_on = not self.mini_on
        if self.mini_on:
            self.root.withdraw()
            self.mini.deiconify()
            self.mini.lift()
        else:
            self.mini.withdraw()
            self.root.deiconify()
            self.root.lift()
        self.cfg["mini"] = self.mini_on

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--build-prices":  # used by the release build
        print(update_prices(lambda t, p=None: print(t, flush=True), path=sys.argv[2]))
    else:
        App().run()
