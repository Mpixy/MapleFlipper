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

VERSION = "0.75"
HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(os.environ.get("LOCALAPPDATA", HERE), "MapleFlipper")
os.makedirs(DATA, exist_ok=True)
PRICES_FILE = os.path.join(DATA, "prices.json")
CUSTOM_FILE = os.path.join(DATA, "custom_prices.txt")
CONFIG_FILE = os.path.join(DATA, "config.json")
LOG_FILE = os.path.join(DATA, "mesos_log.csv")
LEARN_FILE = os.path.join(DATA, "learned.json")
STATS_FILE = os.path.join(DATA, "flips.json")
SHOPS_FILE = os.path.join(DATA, "shops.json")  # shops you visited: seller -> when, best profit
MARKET_FILE = os.path.join(DATA, "market_seen.json")  # item -> the last 20 prices seen in shops [unit, shop, when]
MARKET_KEEP = 20  # prices remembered per item
MARKET_MIN = 5  # prices needed before the app compares (fewer says too little)
CHEAP = 0.8  # under 80% of the lowest of those prices = a blue 'cheap' alert
CHEAP_SAVE = 1000  # ...and at least this much under it (94 instead of 125 is no news)
REPO_ZIP = "https://github.com/Mpixy/MapleFlipper/archive/refs/heads/main.zip"
RELEASE = "https://github.com/Mpixy/MapleFlipper/releases/download/latest/"
FROZEN = getattr(sys, "frozen", False)  # running as MapleFlipper.exe
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
    except FileNotFoundError:
        return default
    except Exception:  # damaged: keep it aside (not overwritten) and start fresh
        try:
            os.replace(path, path + time.strftime(".bad-%m%d-%H%M%S"))
        except OSError:
            pass
        return default


def save_json(path, data):
    """Write to a temp file and swap it in: a crash / power cut mid-save can't wipe what
    the app learned."""
    tmp = f"{path}.{threading.get_ident()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=None if path == MEMORY_FILE else 1)
    os.replace(tmp, path)


def log_error(etype, value, tb):
    """pythonw has no console, so keep crashes in a file we can look at."""
    import traceback
    with open(os.path.join(DATA, "error_log.txt"), "a", encoding="utf-8") as f:
        f.write(time.strftime("\n%Y-%m-%d %H:%M:%S\n") + "".join(traceback.format_exception(etype, value, tb)))


sys.excepthook = log_error
threading.excepthook = lambda a: log_error(a.exc_type, a.exc_value, a.exc_traceback)


# ---------------------------------------------------------------- debug log (for Claude, not shown)
# Everything the app saw and decided goes to debug/log.jsonl (+ small pictures of the shop list
# in debug/shots). 'Save debug zip' packs it up, so a complaint can be traced exactly.
DEBUG_DIR = os.path.join(DATA, "debug")
SHOTS_DIR = os.path.join(DEBUG_DIR, "shots")
os.makedirs(SHOTS_DIR, exist_ok=True)
_dlock = threading.Lock()


def dlog(kind, **data):
    try:
        line = json.dumps({"t": time.strftime("%H:%M:%S") + f".{int(time.time() * 1000) % 1000:03d}",
                           "d": time.strftime("%Y-%m-%d"), "k": kind, **data},
                          ensure_ascii=False, default=lambda o: o.item() if hasattr(o, "item") else str(o))
        path = os.path.join(DEBUG_DIR, "log.jsonl")
        with _dlock:
            if os.path.exists(path) and os.path.getsize(path) > 8_000_000:
                os.replace(path, os.path.join(DEBUG_DIR, "log.old.jsonl"))
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except Exception:
        pass  # logging must never break the app


def save_shot(tag, img, keep=200):
    """A small JPEG of what was read, named by time, so a log line can be matched to a picture."""
    try:
        import cv2
        name = time.strftime("%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}-{tag}.jpg"
        cv2.imwrite(os.path.join(SHOTS_DIR, name), img, [cv2.IMWRITE_JPEG_QUALITY, 85])
        files = sorted(os.listdir(SHOTS_DIR))
        for old in files[:-keep]:
            os.remove(os.path.join(SHOTS_DIR, old))
        return name
    except Exception:
        return None


# ---------------------------------------------------------------- memory (learns as you play)
MEMORY_FILE = os.path.join(DATA, "memory.json")


def pix_key(img):
    import hashlib
    return hashlib.md5(img.tobytes() + str(img.shape).encode()).hexdigest()[:20]


class Memory:
    """What the app has seen before, by exact pixels - the game draws the same price text and
    the same item icon identically every time, so once read for sure, it is known for good:
    price: picture of a 'N for X mesos' line -> (N, X)  (no OCR, no doubt next time)
    icon:  picture of an item icon -> votes for its name  (beats a misread name)
    bought: item -> how often you bought it  (those are always read extra carefully)"""

    def __init__(self):
        d = load_json(MEMORY_FILE, {})
        self.price = d.get("price", {})
        self.icon = d.get("icon", {})
        self.bought = d.get("bought", {})
        self.names = d.get("names", {})  # picture of a name line -> [text, matched for sure]
        self.dirty, self.saved = False, time.time()
        self.lock = threading.Lock()  # scans (worker thread) write while the window saves

    def icon_name(self, key):
        votes = self.icon.get(key) if key else None
        if not votes:
            return None
        name, n = max(votes.items(), key=lambda kv: kv[1])
        return name if n >= 3 and n >= 0.8 * sum(votes.values()) else None

    def learn_icon(self, key, name):
        if key:
            with self.lock:
                v = self.icon.setdefault(key, {})
                v[name] = min(v.get(name, 0) + 1, 50)
                self.dirty = True

    def learn_price(self, key, qty, price):
        if self.price.get(key) != [qty, price]:
            with self.lock:
                self.price[key] = [qty, price]
                self.dirty = True

    def learn_name(self, key, text, known):
        if self.names.get(key) != [text, known]:
            with self.lock:
                self.names[key] = [text, known]
                self.dirty = True

    def save(self, force=False):
        if self.dirty and (force or time.time() - self.saved > 30):
            with self.lock:
                for dct in (self.price, self.icon):
                    while len(dct) > 30000:
                        dct.pop(next(iter(dct)))
                while len(self.names) > 30000:
                    self.names.pop(next(iter(self.names)))
                data = {"price": dict(self.price), "icon": {k: dict(v) for k, v in self.icon.items()},
                        "bought": dict(self.bought), "names": dict(self.names)}
                self.dirty, self.saved = False, time.time()
            save_json(MEMORY_FILE, data)


MEM = None  # created by the app (after the data folder exists)


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
        self.manual = set(d.get("manual", []))  # prices the user typed in: always trusted
        self.nx = set(d.get("nx", []))  # cash (NX) items: seen with the red C coin, NPCs don't buy them
        self.market = d.get("market", {})  # typical market range the user typed in: item -> [low, high]

    def save(self):
        save_json(LEARN_FILE, {"prices": self.prices, "aliases": self.aliases,
                               "blocked": self.blocked, "manual": sorted(self.manual),
                               "nx": sorted(self.nx), "market": self.market})

    def mark_nx(self, item):
        if item not in self.nx:
            self.nx.add(item)
            self.save()
            return True
        return False

    def drop_wild(self, db):
        """Forget learned prices that are 3x off MeowDB: those came from a sale matched to the
        wrong item (e.g. Lemona 1,800 learned as 11,800), not from a real price change."""
        bad = [n for n, p in self.prices.items() if n not in self.manual and db.get(n)
               and not db[n] / 3 <= p <= db[n] * 3]
        for n in bad:
            del self.prices[n]
        if bad:
            self.save()
        return bad

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
        self.nx.discard(item)  # an NPC paid for it: not a cash item
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


def parse_price(text):
    """'1 for 2,999 mes0s' -> (qty, price, well_formed) or None."""
    m = PRICE_LINE.search(text.replace(".,", ",").replace(",.", ","))
    if not m or not to_int(m.group(1)) or not to_int(m.group(2)):
        return None
    raw = m.group(2).strip(",.")
    return to_int(m.group(1)), to_int(raw), bool(GROUPED.match(raw))


def read_price(crop, big=False):
    """Read a 'N for X mesos' line (big=True: enlarged first - a second opinion)."""
    if big:
        import cv2  # comes with rapidocr
        crop = cv2.resize(crop, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    return parse_price(read_line(np.ascontiguousarray(crop)))


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
    1. Pictures seen before (memory): price / name known at once, no OCR.
    2. The price is checked against the width of the line on screen (OCR drops digits:
       '2,999' -> '299'); doubtful rows get more tries (bigger, black on white) right away,
       instead of you pressing F8 again and again.
    worth(name, qty, price) -> True: rows worth buying, always double-checked."""
    global last_faded
    t0 = time.time()
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
        pkey = pix_key(crops[0])
        mem = MEM.price.get(pkey) if MEM else None
        if mem:
            got, ptxt = (mem[0], mem[1], True), "memory"
        else:
            ptxt = read_line(crops[0])
            got = parse_price(ptxt) or read_price(crops[0], big=True)
            if not got:
                continue  # empty slot / tooltip in the way
        icon = img[ny + 2:ny + 34, nx - 62:nx - 16] if nx >= 62 else None
        ikey = pix_key(np.ascontiguousarray(icon)) if icon is not None and icon.size else None
        nkey = pix_key(crops[1])
        nmem = MEM.names.get(nkey) if MEM else None  # this exact name picture was read before
        name = nmem[0] if nmem else read_line(crops[1])
        # the icon only helps when the text isn't a known item (it once learned 'Saw' as 'Sai')
        iname = MEM.icon_name(ikey) if MEM and match_hint(name) is False else None
        name = iname or name
        if re.search(r"[A-Za-z]{2}", name):
            rect = (nx - 50, ny - 4, width + 54, py + ph + 4 - (ny - 4))
            found.append((name, got, crops, rect, ink_width(crops[0]), pkey, bool(mem), ikey, iname, ptxt,
                          nkey, nmem))
    # how wide one character is: the median over this page and earlier pages
    here = [w / len(f"{q} for {p:,} mesos") for _, (q, p, _), _, _, w, *_ in found if w]
    ref = float(np.median(_char_px[-30:] + here)) if len(_char_px + here) >= 3 else None
    items = []
    for name, (qty, cost, formed), crops, rect, w, pkey, from_mem, ikey, iname, ptxt, nkey, nmem in found:
        tries = []
        if from_mem:
            fits = sure = True
        else:
            fits = width_fits(w, qty, cost, ref)
            sure = formed and fits and ref is not None
            if not sure or (worth and worth(name, qty, cost)):
                for how in ("big", "big3", "bw"):  # more ways to read it, until one fits
                    alt = read_variant(crops[0], how)
                    tries.append((how, alt and alt[:2]))
                    if not alt:
                        continue
                    alt_fits = width_fits(w, alt[0], alt[1], ref)
                    if alt[:2] == (qty, cost):
                        if ref is None:
                            sure = formed  # no width yet: two identical reads will do
                        if sure:
                            break
                        continue
                    if alt_fits and alt[2] and not (fits and formed):
                        (qty, cost, formed), fits, sure = alt, True, ref is not None
                        break
                    if alt_fits and fits:
                        sure = False  # two different readings that both look right: check it
                        break
                    if sure:
                        break
        if not iname and not nmem and match_hint(name) is False:  # unknown name: try harder (once)
            for how in ("big", "big3", "bw"):
                name2 = read_variant(crops[1], how, text=True)
                tries.append(("name-" + how, name2))
                if name2 and match_hint(name2):
                    name = name2
                    break
        items.append((name, qty, cost, rect, sure))
        if w and fits and formed and not from_mem:
            _char_px.append(w / len(f"{qty} for {cost:,} mesos"))
        if MEM and sure and not from_mem:
            MEM.learn_price(pkey, qty, cost)
        real, score = matcher(name) if matcher else (None, 0)
        if MEM and not iname and not nmem:  # next time: no OCR, no retries for this name
            MEM.learn_name(nkey, name, bool(real))
        if MEM and real and score == 100 and sure:
            MEM.learn_icon(ikey, real)
        dlog("row", name=name, icon_name=iname, real=real, score=score, price_text=ptxt, qty=qty,
             price=cost, sure=bool(sure), width=w, char_px=round(ref, 2) if ref else None,
             mem=from_mem, name_mem=bool(nmem), tries=tries)
    del _char_px[:-60]
    dlog("rows", n=len(items), faded=last_faded, ms=int((time.time() - t0) * 1000))
    return items


def read_variant(crop, how, text=False):
    """Other ways to read the same line: enlarged 2x / 3x, or black-on-white."""
    import cv2
    if how == "big":
        im = _big(crop)
    elif how == "big3":
        im = cv2.resize(crop, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
    else:
        g = cv2.cvtColor(_big(crop), cv2.COLOR_BGR2GRAY)
        _, bw = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        im = cv2.cvtColor(bw, cv2.COLOR_GRAY2BGR)
    t = read_line(np.ascontiguousarray(im))
    return t if text else parse_price(t)


def _big(crop):
    import cv2
    return cv2.resize(crop, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)


name_check = None  # set by the app: name -> True (known item) / False (unknown)
matcher = None  # set by the app: name -> (item, score)


def match_hint(name):
    return name_check(name) if name_check else None


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
    """Read the seller's name tag under the avatar (left of the buttons): find the dark blue
    tag itself, then read its white text enlarged as black on white - much steadier."""
    import cv2
    x, y = int(btn[0]), int(btn[1])
    y0, x0 = max(y + 20, 0), max(x - 380, 0)
    area = img[y0:y + 150, x0:max(x - 60, 0)]
    if not area.size:
        return ""
    b, g, r = (area[:, :, k].astype(int) for k in range(3))
    navy = (abs(b - 82) < 25) & (abs(g - 54) < 25) & (abs(r - 31) < 25)
    rows = np.nonzero(navy.sum(axis=1) > 60)[0]
    if _ocr is None:
        ocr(np.zeros((32, 32, 3), np.uint8))  # load the reader
    if len(rows) < 8:  # no tag found: fall back to the old fixed spot
        tag = img[max(y + 62, 0):y + 95, max(x - 330, 0):max(x - 110, 0)]
        return read_line(np.ascontiguousarray(tag)) if tag.size else ""
    split = np.nonzero(np.diff(rows) > 1)[0]  # only the first dark-blue band (the name tag)
    if len(split):
        rows = rows[:split[0] + 1]
    if len(rows) < 8:
        return ""
    band = navy[rows[0]:rows[-1] + 1]
    cols = np.nonzero(band.sum(axis=0) > len(band) * 0.3)[0]
    tag = area[rows[0] + 1:rows[-1], cols[0] + 2:cols[-1] - 1]
    gray = cv2.cvtColor(np.ascontiguousarray(tag), cv2.COLOR_BGR2GRAY)
    gray = cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    text = np.where(gray > 140, 0, 255).astype(np.uint8)
    text = cv2.copyMakeBorder(text, 8, 8, 8, 8, cv2.BORDER_CONSTANT, value=255)
    return read_line(np.ascontiguousarray(cv2.cvtColor(text, cv2.COLOR_GRAY2BGR))).strip(" .")


def find_cash_badge(img):
    """The red 'C' coin the game draws on a cash (NX) item's picture in its tooltip (BGR screen
    grab). Returns the coin's centre, or None. Red disk + orange rim + light 'C' inside - the
    red/white target on the 'Hit Me' shirt has no orange rim, so it doesn't count."""
    import cv2
    a = img.astype(np.int16)
    b, g, r = a[..., 0], a[..., 1], a[..., 2]
    red = ((r > 170) & (g < 80) & (b < 80)).astype(np.uint8)
    n, _, st, cen = cv2.connectedComponentsWithStats(red, connectivity=8)
    for i in range(1, n):
        x, y, w, h, area = st[i]
        if not (18 <= w <= 50 and 18 <= h <= 50 and 0.75 <= w / h <= 1.33 and 0.3 <= area / (w * h) <= 0.9):
            continue
        y0, y1, x0, x1 = max(y - 4, 0), y + h + 4, max(x - 4, 0), x + w + 4
        rr, gg, bb = r[y0:y1, x0:x1], g[y0:y1, x0:x1], b[y0:y1, x0:x1]
        orange = ((rr > 190) & (gg > 100) & (gg < 210) & (bb < 90)).sum()
        inner = (r[y:y + h, x:x + w] > 200) & (g[y:y + h, x:x + w] > 150)  # the 'C'
        if orange >= w and inner.sum() >= 6:
            return int(cen[i][0]), int(cen[i][1])
    return None


def tooltip_title(img, badge):
    """The tooltip's first line (the item's name), 90-140 px above the coin."""
    bx, by = badge
    return white_title(img, bx - 130, by - 145, bx + 280, by - 88)


def cursor_title(img, cx, cy):
    """The name at the top of the tooltip the game shows under the mouse (cursor in image
    pixels): the tooltip's title sits 45-95 px below and right of the pointer."""
    return white_title(img, cx - 10, cy + 25, cx + 380, cy + 110)


def white_title(img, x0, y0, x1, y1):
    """The first line of white letters on the dark blue tooltip inside this area."""
    import cv2
    crop = img[max(y0, 0):max(y1, 0), max(x0, 0):max(x1, 0)]
    if crop.size == 0:
        return ""
    px = crop.astype(np.int16)
    blue = px[..., 0] - px[..., 2]  # the tooltip: dark blue, lighter where a white window is behind it
    navy = ((blue > 40) | ((blue > 25) & (px.max(axis=2) < 190))).astype(np.uint8)
    lit = crop.min(axis=2) > 215
    # white letters with the dark blue tooltip right next to them - not the white chat box
    # or the white glove of the mouse pointer
    white = lit & (cv2.dilate(navy, np.ones((5, 5), np.uint8)) > 0)
    # the title is the first line of the tooltip: start at the tooltip's top edge
    top = np.where(navy.mean(axis=1) > 0.3)[0]
    if not len(top):
        return ""
    y0 = top[0]
    rows = np.where(white[y0:y0 + 45].sum(axis=1) >= 3)[0] + y0
    blocks = np.split(rows, np.where(np.diff(rows) > 3)[0] + 1) if len(rows) else []
    block = next((b for b in blocks if len(b) >= 8), None)
    if block is None:
        return ""
    r0, r1 = max(block[0] - 3, 0), block[-1] + 4
    inside = np.where(navy[r0:r1].mean(axis=0) > 0.3)[0]  # the tooltip's width on this line
    if not len(inside):
        return ""
    cols = np.where(lit[r0:r1, inside[0]:inside[-1] + 1].any(axis=0))[0] + inside[0]
    if not len(cols) or cols[-1] - cols[0] < 30:
        return ""
    band = lit[r0:r1, cols[0]:cols[-1] + 1]
    band = np.pad(band, ((10, 10), (30, 30)))  # the reader needs a margin around the letters
    clean = np.where(band[..., None], 255, 40).astype(np.uint8).repeat(3, 2)
    clean = np.ascontiguousarray(cv2.resize(clean, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC))
    with _ocr_lock:
        res, _ = _ocr(clean, use_det=False, use_cls=False)
    # some versions give the words one by one: put them back together, left to right
    words = sorted(((r[0][0][0] if len(r) == 3 else 0), r[-2]) for r in res or [] if isinstance(r[-2], str))
    return re.sub(r"^[^A-Za-z0-9]+", "", " ".join(w for _, w in words)).strip()


TOTAL_LINE = re.compile(r"total\s*price\s*is\s*(\d[\d,. ]*)", re.I)  # ('7 ,670': the reader adds spaces)


def find_dialog(img):
    """The game's blue question box ('Are you sure you want to buy it?' / 'The total price is
    7,670 mesos.'): its box in the BGR image, or None."""
    import cv2
    a = img[::2, ::2].astype(np.int16)
    b, g, r = a[..., 0], a[..., 1], a[..., 2]
    m = ((abs(b - 187) < 14) & (abs(g - 136) < 14) & (abs(r - 70) < 16)).astype(np.uint8)
    n, _, st, _ = cv2.connectedComponentsWithStats(m, connectivity=8)
    for i in range(1, n):
        x, y, w, h, area = st[i]
        if w >= 120 and h >= 25 and area >= 0.6 * w * h:
            return int(x * 2), int(y * 2), int(w * 2), int(h * 2)
    return None


def dialog_total(img):
    """'The total price is 7,670 mesos.' -> 7670 (None: no such box on the screen)"""
    box = find_dialog(img)
    if not box:
        return None
    x, y, w, h = box
    for o in ocr(np.ascontiguousarray(img[y:y + h, x:x + w])):
        hit = TOTAL_LINE.search(o["text"])
        if hit:
            return to_int(hit.group(1))
    return None


def covered(img, rect):
    """Is the dark blue tooltip lying over this row (so its text can't be read)?"""
    x, y, w, h = rect
    px = img[max(y, 0):y + h, max(x, 0):x + w].astype(np.int16)
    if px.size == 0:
        return False
    blue = px[..., 0] - px[..., 2]  # (the rows' own pale blue-grey is under 30)
    return ((blue > 60) | ((blue > 35) & (px.max(axis=2) < 150))).mean() > 0.15


VOLUME = [0.3]  # 0..1, the volume slider


def play(notes, ms=100):
    """Play a few short tones at the slider's volume (winsound.Beep is always full blast).
    The tones become a small WAV file, played without waiting."""
    import winsound
    import wave
    if VOLUME[0] <= 0:
        return
    rate = 22050
    t = np.arange(int(rate * ms / 1000)) / rate
    fade = np.minimum(1, np.minimum(t, t[::-1]) * 200)  # no clicks at the start / end
    wav = np.concatenate([np.sin(2 * np.pi * f * t) * fade for f in notes])
    wav = (wav * 32000 * VOLUME[0] ** 2).astype(np.int16)  # squared: the slider feels even
    path = os.path.join(DATA, f"sound-{'-'.join(map(str, notes))}-{ms}-{int(VOLUME[0] * 100)}.wav")
    if os.path.exists(path):  # (never rewrite a file that may be playing right now)
        winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
        return
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(wav.tobytes())
    winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)


def soft_ping():
    """A short, quiet 'tik' for a cheap market find."""
    play([1320], 60)


def ago(t):
    m = int((time.time() - t) // 60)
    return "just now" if m < 1 else f"{m} min ago" if m < 60 else f"{m // 60}h {m % 60:02d}m ago"


def shop_mesos(img, btn):
    """Your own mesos, shown in the player-shop window under the BUY ITEM button. Read it while
    you shop: a drop by an item's price = you bought it (no 'Set mesos area' needed).
    The white number box is found by its colour, so a bigger / smaller window still works
    (the box's border was once read as an extra '1': '171,0571')."""
    x, y = int(btn[0]), int(btn[1])
    area = img[max(y + 60, 0):y + 130, max(x - 90, 0):x + 140]
    if area.shape[0] < 20 or area.shape[1] < 120:
        return None
    white = (area[:, :, :3] > 235).all(axis=2)
    rows = np.nonzero(white.mean(axis=1) > 0.45)[0]
    runs = np.split(rows, np.nonzero(np.diff(rows) > 1)[0] + 1) if len(rows) else []
    rows = max(runs, key=len) if len(runs) else rows  # the tallest white band = the number box
    if len(rows) >= 10:
        band = white[rows[0]:rows[-1] + 1]
        cols = np.nonzero(band.mean(axis=0) > 0.5)[0]
        field = area[rows[0]:rows[-1] + 1, cols[0]:cols[-1] + 1] if len(cols) > 60 else None
    else:
        field = None
    if field is None:
        field = img[max(y + 80, 0):y + 107, max(x - 55, 0):x + 110]
    t = read_line(np.ascontiguousarray(field)).replace(" ", "")
    m = re.search(r"\d{1,3}(?:[,.]\d{3})+|\d{1,6}", t)
    return to_int(m.group(0)) if m else None


def clock(sec):
    sec = int(sec)
    return f"{sec // 3600}:{sec // 60 % 60:02d}:{sec % 60:02d}"


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


def ignored(name):
    """Arrows: an NPC pays next to nothing for them - not even listed."""
    n = norm(name)
    return "arrowsfor" in n or "arrowfor" in n


def npc_value(name, unit_price, qty):
    """Stars/bullets are one rechargeable item: the shop shows the ammo count, not a stack.
    Arrows: '500 for 200 mesos' looks like 500 x 1 meso, but an NPC pays next to nothing
    for a pile of arrows - never count them as a stack (they just show as a loss)."""
    if any(k in norm(name) for k in ("throwingstar", "bullet", "arrowsfor", "arrowfor")):
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
    allowed = 3 if len(key) >= 18 else 2 if len(key) >= 10 else 1  # 'Single aring' = Single Earring
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

HELP_TEXT = """BLUE = CHEAP
 The app remembers the last 20 prices it saw for every item, in every shop you open.
 A blue row / frame + a quiet tik = this shop sells it under 80% of the LOWEST of those 20
 (it starts comparing once it has 5 prices, and only for 1,000+ mesos under it).
 Then that price joins the 20, so the lowest moves.
 The 'NPC pays' column shows: low <lowest> (<how many prices>).
 💎 = NX item (NPCs don't buy it) - learned from the red C coin in the tooltip.

FIND PROFITABLE ITEMS
1. Walk into a player shop in the game.
2. Press F8 (or the big SCAN button).
3. Green rows = buy it and sell it to any NPC for profit.
4. Scroll down in the shop and press F8 again - the list keeps growing.
5. 'Clear list' before visiting the next shop.

TRACK YOUR MESOS  (a timer runs while tracking: time, profit, profit per hour)
1. Open your inventory in the game.
2. Click 'Set mesos area' and drag a box around the mesos number.
3. Click 'Start tracking'. Every buy / NPC sale is logged with the profit.

With 'Auto' on you don't need F8 at all: the app notices when you open
a player shop and scans it, and rescans when you scroll.

FLIPS
'Stats' (or click the flips line) shows today / 7 days / all time, your best
items and the sellers you made the most from. The mini bar shows the profit
of this session and per hour.
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

SHOPS YOU ALREADY CHECKED
Walk into a shop you checked in the last 12 hours and the app tells you
when, and what the best deal was ('Shops' button for the full list).

WATCH LIST
'👀 Watch' (or right-click a row): items you always want to hear about.
Purple frame + sound when one is in a shop, profitable or not.

KEYS: F8 scan · F9 mini/full window · F10 clear the list
     F11 "this is wrong!" - saves the moment for Claude ('Debug zip' sends it)
'⟳ Restart' if the app ever gets stuck - nothing is lost.

YOUR MESOS ARE READ FROM THE SHOP WINDOW
While a shop is open the app reads your mesos (the number under BUY ITEM).
Mesos drop by an item's price = you bought it: blue 'BOUGHT' row, and it
won't ask you to buy it again. No setup needed ('Set mesos area' is only
for selling to NPCs outside a shop).

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
        # things bought for yourself (a loss at the NPC) were never flips: drop them
        self.bought[:] = [b for b in self.bought if b[3] is None or b[3] - b[4] > 0]
        self.shop_box = None
        self.scanned = None
        self.rows = []
        self.incomplete = False
        self.incomplete_since = 0.0
        self.shop_btn = None
        self.shop_btn_abs = None  # the BUY ITEM button on the screen (for the saved layout)
        self.layout_try = False  # this shop was opened with the saved layout (no text detection)
        self.redetect = False
        self.shop_sig = None
        self.shop_seen = 0.0
        self.busy_since = 0.0
        self.prices = load_prices()
        self.wild = self.learned.drop_wild(self.prices)
        global name_check, matcher, MEM
        name_check = lambda n: match_name(n, self.names, self.learned)[0] is not None  # noqa: E731
        matcher = lambda n: match_name(n, self.names, self.learned)  # noqa: E731
        MEM = Memory()
        dlog("start", version=VERSION, prices=len(self.prices), learned=len(self.learned.prices),
             memory_prices=len(MEM.price), memory_icons=len(MEM.icon))
        self.market = load_json(MARKET_FILE, {})  # prices seen in shops: item -> [[unit, shop, when], ...]
        self.cheap_ref = {}  # (item key, shop) -> (unit price, lowest seen before it, how many) when it is cheap, else None
        self.cheap_seen = set()  # (item key, shop) already pinged as cheap
        self.badge_last = None
        self.names = self.all_names()
        self.seen = {}
        self.seen_shop = {}  # item key -> seller name
        self.unsure = set()  # item keys whose price was read differently twice: never 'profitable'
        self.bought_keys = {}  # item keys you bought (mesos tracker) -> mesos paid
        self.cur_shop = ""
        self.shops = load_json(SHOPS_FILE, {})
        self.session_t = time.time()
        self.track_t0 = None  # when 'Start tracking' was pressed (the timer)
        self.track_flips = 0  # profit of the flips finished since then
        self.on_page = set()  # item keys on the shop page you are looking at right now
        self.seen_t = {}  # item key -> when it was last on screen
        self.count = {}  # item key -> how many identical rows the shop page has (Lemona ×2)
        self.watch = self.cfg.get("watch", [])  # item names (or parts) to always tell you about
        self.watch_seen = set()
        self.odd_name = set()  # keys whose name looked suspicious (see apply_scan)
        self.session_flips = 0  # profit of the flips finished since the app started
        self.revisit = None  # (when, best profit) of the current shop's last visit
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
        # load the text reader now, not when the first shop opens (that cost 1-3 s right then)
        threading.Thread(target=lambda: ocr(np.zeros((32, 96, 3), np.uint8)), daemon=True).start()
        threading.Thread(target=self.auto_loop, daemon=True).start()
        self.root.after(1000, self.quit_requested)
        if len(self.prices) < 500 or os.path.exists(PRICES_FILE + ".progress"):  # first run / cut off
            self.root.after(300, self.update_db)
        else:
            self.say(f"Ready - {len(self.prices):,} item prices loaded. Open a player shop and press F8.")
        if self.wild:
            self.say(f"Fixed {len(self.wild)} wrongly learned prices (back to MeowDB): {', '.join(self.wild[:8])}")
        if not os.path.isdir(os.path.join(HERE, ".git")):  # (a developer's copy updates with git)
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
        ttk.Button(bar, text="Clear (F10)", command=self.clear).pack(side="left")
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
        self.volume = tk.IntVar(value=self.cfg.get("volume", 30))
        VOLUME[0] = self.volume.get() / 100
        ttk.Label(bar, text="🔊").pack(side="left", padx=(6, 0))
        vol = ttk.Scale(bar, from_=0, to=100, length=70, variable=self.volume,
                        command=lambda v: VOLUME.__setitem__(0, float(v) / 100))
        vol.pack(side="left")
        vol.bind("<ButtonRelease-1>", lambda e: (self.cfg.__setitem__("volume", self.volume.get()),
                                                 save_json(CONFIG_FILE, self.cfg), soft_ping()))
        ttk.Button(bar, text="Update prices", command=self.update_db).pack(side="right")
        ttk.Button(bar, text="? Help", command=self.show_help).pack(side="right", padx=6)
        ttk.Button(bar, text="▭ Mini (F9)", command=self.toggle_mini).pack(side="right")
        ttk.Button(bar, text="Shops", command=self.shops_window).pack(side="right", padx=(0, 6))
        ttk.Button(bar, text="Stats", command=self.stats_window).pack(side="right", padx=(0, 6))
        ttk.Button(bar, text="👀 Watch", command=self.watch_dialog).pack(side="right", padx=(0, 6))
        ttk.Button(top, text="⟳ Restart", command=self.restart).pack(side="right")
        ttk.Button(top, text="Debug zip", command=self.debug_zip).pack(side="right", padx=4)
        ttk.Button(top, text="Check for update", command=lambda: self.check_update(manual=True)
                   ).pack(side="right", padx=4)

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
        self.tree.tag_configure("watch", background="#e7d4f7")
        self.tree.tag_configure("cheap", background="#c7e2ff")
        self.tree.tag_configure("nx", foreground="#7a5cc0")
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
        self.timer_lbl = ttk.Label(row, font=("Consolas", 12, "bold"), foreground="#1a5fb4", text="")
        self.timer_lbl.pack(side="left", padx=8)
        self.meso_lbl = ttk.Label(box, font=("Segoe UI", 12, "bold"), text="Mesos: -")
        self.meso_lbl.pack(anchor="w", padx=6)
        self.tosell_lbl = ttk.Label(box, foreground="#b35c00", wraplength=520, cursor="hand2")
        self.tosell_lbl.pack(anchor="w", padx=6)
        self.tosell_lbl.bind("<Button-1>", lambda e: self.sell_dialog())
        self.stats_lbl = ttk.Label(box, foreground="#555", cursor="hand2")
        self.stats_lbl.pack(anchor="w", padx=6)
        self.stats_lbl.bind("<Button-1>", lambda e: self.stats_window())
        self.hist = tk.Listbox(box, height=4, font=("Consolas", 10))
        self.hist.pack(fill="x", padx=4, pady=4)



    def say(self, text, pct=None):
        if pct is None:
            dlog("say", text=text)
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

    def latest_version(self):
        """The newest version you can update to: for the .exe the one already built on GitHub
        (a change is built a minute or two after it is merged), else the newest code."""
        if FROZEN:
            try:
                v = requests.get(RELEASE + "version.txt", timeout=15)
                if v.status_code == 200 and re.fullmatch(r"[\d.]+", v.text.strip()):
                    return v.text.strip()
            except requests.RequestException:
                pass
        src = requests.get(REPO_SOURCE, timeout=15).text
        m = re.search(r'VERSION = "([\d.]+)"', src)
        return m.group(1) if m else None

    def check_update(self, manual=False):
        """Look for a newer version (every 20 minutes, and when you press the button)."""
        def work():
            try:
                new = self.latest_version()
            except Exception as e:
                if manual:
                    self.say(f"Couldn't check for updates: {e}")
                return
            newer = new and tuple(map(int, new.split("."))) > tuple(map(int, VERSION.split(".")))
            dlog("update_check", have=VERSION, latest=new, manual=manual)
            if newer:
                self.root.after(0, lambda: (self.update_btn.config(text=f"⬆ Update to v{new}"),
                                            self.update_btn.pack(side="left", padx=8)))
                if manual:
                    self.root.after(0, lambda: messagebox.askyesno(
                        "MapleFlipper", f"Version {new} is out (you have {VERSION}).\nUpdate now? "
                        "It restarts by itself - nothing you learned is lost.") and self.self_update())
            elif manual:
                self.say(f"✓ You have the latest version (v{VERSION}).")
        threading.Thread(target=work, daemon=True).start()
        if not manual:
            self.root.after(20 * 60 * 1000, self.check_update)

    def self_update(self):
        """Download the latest version over this one and restart."""
        import io
        import subprocess
        import zipfile
        self.say("Updating... (a few seconds)")
        self.root.update()
        try:
            if FROZEN:
                # the .exe can't overwrite itself while running: unpack next to it, then a small
                # script waits for this window to close, copies the new files in and starts it
                z = zipfile.ZipFile(io.BytesIO(requests.get(RELEASE + "MapleFlipper-Windows.zip",
                                                            timeout=180).content))
                new = os.path.join(DATA, "update_new")
                import shutil
                shutil.rmtree(new, ignore_errors=True)
                z.extractall(new)
                here = os.path.dirname(sys.executable)
                bat = os.path.join(DATA, "update.bat")
                with open(bat, "w", encoding="utf-8") as f:
                    f.write(f"""@echo off
:wait
tasklist /fi "PID eq {os.getpid()}" | find "{os.getpid()}" >nul && (timeout /t 1 /nobreak >nul & goto wait)
robocopy "{new}" "{here}" /E /NFL /NDL /NJH /NJS /NP >nul
start "" "{sys.executable}"
""")
                dlog("update", to="release", folder=here)
                subprocess.Popen(["cmd", "/c", bat], creationflags=0x08000000)  # no console window
                self.on_close_save()
                os._exit(0)
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
        subprocess.Popen([sys.executable, os.path.join(HERE, "maple_flipper.py")])
        self.on_close()

    def show_help(self):
        messagebox.showinfo("How to use MapleFlipper", HELP_TEXT)

    def auto_loop(self):
        """Finds a player shop by itself (orange BUY ITEM button), rescans it when the list
        changes (scrolling) and notices when it closes."""
        last, tried, checked, misses = None, None, 0.0, 0
        m_pending, m_hits = None, 0
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
                if box and time.time() - checked > 0.25:  # is the shop still open / still there?
                    checked = time.time()
                    screen = grab()
                    btn = find_shop_button(screen[:, :, ::-1])  # screen grabs are BGR
                    try:
                        self.check_cash(screen)
                        self.check_tooltip(screen, mon)
                        self.check_selected(screen, mon)
                    except Exception as e:
                        log_error(type(e), e, e.__traceback__)
                    v = shop_mesos(screen, btn) if btn else None
                    if v is not None:  # your mesos in the shop window: 2 equal reads in a row
                        self.shop_mesos_t = time.time()
                        m_hits = m_hits + 1 if v == m_pending else 1
                        m_pending = v
                        old = self.mesos
                        glitch = old is not None and (lost_digits(str(v), str(old)) or lost_digits(str(old), str(v)))
                        if m_hits == (4 if glitch else 2) and v != old:
                            self.root.after(0, self.on_mesos, v)
                    moved = btn and self.shop_btn and (abs(btn[0] - self.shop_btn[0]) > 40
                                                       or abs(btn[1] - self.shop_btn[1]) > 40)
                    misses = misses + 1 if (not btn or moved) else 0
                    if misses >= 2:  # mouse hover can hide the button for a moment
                        misses = 0
                        self.shop_box, last, tried = None, None, None
                        self.root.after(0, self.hide_marks)
                        self.root.after(0, self.left_shop)
                        self.say("Shop closed - waiting for the next shop.")
                        continue
                if not box:  # no shop known: look for one (cheap colour check, no OCR)
                    time.sleep(0.08)
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
                    near = self.shop_btn is not None and abs(btn[0] - self.shop_btn[0]) < 40 \
                        and abs(btn[1] - self.shop_btn[1]) < 40
                    # name unreadable this time: the same spot a moment ago is the same shop
                    same_shop = time.time() - self.shop_seen < 60 and (
                        same_seller(sig, self.shop_sig) or (near and not (sig and self.shop_sig)))
                    self.shop_btn, self.shop_sig = btn, sig or (self.shop_sig if same_shop else "")
                    x, y = int(btn[0]) + mon["left"], int(btn[1]) + mon["top"]
                    self.shop_btn_abs = (x, y)
                    if not same_shop:
                        self.root.after(0, self.new_shop, sig)
                    self.say(f"Shop of {sig or '?'} - reading it...")
                    lay = self.cfg.get("layout2")
                    if lay:  # every shop window looks the same: read the rows right away
                        self.shop_box = {"left": x + lay["dx"], "top": y + lay["dy"],
                                         "width": lay["w"], "height": lay["h"]}
                        self.rows = [[tuple(a[:3]) + (None,) for a in row] for row in lay["rows"]]
                        self.scanned, self.layout_try = None, True
                        self.root.after(0, self.scan, True, self.shop_box)
                    else:
                        # just the item list (left half, under the buttons): less to read = faster
                        cand = {"left": max(x - 420, mon["left"]), "top": max(y + 80, mon["top"]),
                                "width": 580, "height": 520}
                        self.root.after(0, self.scan, True, cand)
                    last = None
                    continue
                if self.redetect:  # the saved layout didn't fit: find the shop the slow way
                    self.redetect, tried, last = False, None, None
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
                now = seller_name(screen, btn)
                if (abs(btn[0] - self.shop_btn[0]) > 40 or abs(btn[1] - self.shop_btn[1]) > 40
                        or (now and self.shop_sig and not same_seller(now, self.shop_sig))):
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
            was_down = was9 = was10 = was11 = False
            while True:
                down = bool(user32.GetAsyncKeyState(0x77) & 0x8000)  # VK_F8
                if down and not was_down:
                    self.root.after(0, self.scan)
                was_down = down
                d9 = bool(user32.GetAsyncKeyState(0x78) & 0x8000)  # VK_F9: mini / full window
                if d9 and not was9:
                    self.root.after(0, self.toggle_mini)
                was9 = d9
                d10 = bool(user32.GetAsyncKeyState(0x79) & 0x8000)  # VK_F10: clear the list
                if d10 and not was10:
                    self.root.after(0, self.clear)
                    self.say("List cleared (F10).")
                was10 = d10
                d11 = bool(user32.GetAsyncKeyState(0x7A) & 0x8000)  # VK_F11: "this is wrong!"
                if d11 and not was11:
                    self.root.after(0, self.complain)
                was11 = d11
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
                self.names = self.all_names()
                if found == 0:
                    self.say("Could not read prices from MeowDB. Send update_log.txt (in %LOCALAPPDATA%\\MapleFlipper) to Claude.")
                else:
                    self.say(f"Prices updated: {total} items.")
            except Exception as e:
                log_error(type(e), e, e.__traceback__)
                self.say(f"Price update failed: {e} - progress is saved, it will continue next time.")
            self.updating = False

        threading.Thread(target=work, daemon=True).start()

    def all_names(self):
        return sorted(set(self.prices) | set(self.learned.prices) | set(self.learned.market) | self.learned.nx)

    def market_range(self, item):
        """(lowest, highest, how many) of the prices you typed in, or of the last 20 seen in shops."""
        if not item:
            return None
        if item in self.learned.market:
            lo, hi = self.learned.market[item]
            return lo, hi, MARKET_KEEP
        seen = [u for u, _, _ in self.market.get(item, [])]
        return (min(seen), max(seen), len(seen)) if len(seen) >= MARKET_MIN else None

    def see_price(self, item, unit):
        """Remember a price seen in a shop (the same shop at the same price counts once);
        only the last 20 are kept, so the lowest moves with the market."""
        lst = self.market.setdefault(item, [])
        if any(s == self.cur_shop and u == unit for u, s, _ in lst):
            return False
        lst.append([unit, self.cur_shop, int(time.time())])
        del lst[:-MARKET_KEEP]
        return True

    def is_nx(self, item):
        return bool(item) and item in self.learned.nx

    def cheap(self, v):
        """Under 80% of the lowest of the last 20 prices seen before it? -> (unit price, that lowest)"""
        real, _, qty, price = v
        pays = self.npc_pays(real, qty) if real else None
        if pays is not None and price <= pays * 1.1:
            return None  # about what an NPC pays anyway: the seller dumps it at the NPC price, no deal
        if real in self.learned.market and qty:  # a price you typed in
            low = self.learned.market[real][0]
            got = (price / qty, low) if price / qty < CHEAP * low else None
        else:
            got = self.cheap_ref.get((real_key(v), self.seen_shop.get(real_key(v), self.cur_shop)))
        return got[:2] if got and (got[1] - got[0]) * qty >= CHEAP_SAVE else None

    def judge_price(self, key, v):
        """The first time a row is seen: compare it with the prices from before, then add it."""
        real, _, qty, price = v
        if not real or not qty or (key, self.cur_shop) in self.cheap_ref:
            return False
        unit = round(price / qty)
        rng = self.market_range(real)
        self.cheap_ref[key, self.cur_shop] = (unit, rng[0], rng[2]) if rng and unit < CHEAP * rng[0] else None
        return self.see_price(real, unit)

    def check_cash(self, screen):
        """A tooltip with the red C coin is on the screen: whatever item it shows is NX. Learned
        for good, by name - next time that item is shown as NX (no NPC price), not as '?'."""
        badge = find_cash_badge(screen)
        if not badge:
            self.badge_last = None
            return
        if self.badge_last and abs(badge[0] - self.badge_last[0]) < 4 and abs(badge[1] - self.badge_last[1]) < 4:
            return  # the same tooltip as a moment ago
        self.badge_last = badge
        title = tooltip_title(screen, badge)
        real, _ = match_name(title, self.names, self.learned) if title else (None, 0)
        item = real or (title if len(norm(title)) >= 4 else None)
        dlog("nx", read=title, item=item, at=badge)
        if item and item not in self.learned.prices and self.learned.mark_nx(item):
            self.names = self.all_names()
            self.say(f"Learned: '{item}' is an NX (cash) item - NPCs don't buy it.")
            self.root.after(0, self.rematch)

    def check_tooltip(self, screen, mon):
        """The mouse rests on a shop row and the game shows its tooltip: the tooltip's title is
        the item's exact name. When the row's own text was read as something else, learn the
        right name for that text - every item, not only NX ones. (Only looks: GetCursorPos.)"""
        import ctypes.wintypes
        pt = ctypes.wintypes.POINT()
        if not ctypes.windll.user32.GetCursorPos(ctypes.byref(pt)):
            return
        cx, cy = pt.x - mon["left"], pt.y - mon["top"]
        row = next((raw for (x, y, w, h), raw, _ in getattr(self, "row_spots", [])
                    if y <= pt.y <= y + h and x - 60 <= pt.x <= x + w), None)
        if not row or (row, cx // 20, cy // 20) == getattr(self, "tip_last", None):
            return
        title = cursor_title(screen, cx, cy)
        if not title:
            return
        self.tip_last = (row, cx // 20, cy // 20)
        real, score = match_name(title, self.names, self.learned)
        was, _ = match_name(row, self.names, self.learned)
        dlog("tooltip", row=row, title=title, item=real, score=score, was=was)
        # the title must really be a known item (a clean read), and must look like the row
        if not real or score < 90 or real == was:
            return
        if Levenshtein.distance(norm(row), norm(real)) > max(3, len(norm(real)) // 3):
            return  # the tooltip of another row (the mouse moved on): don't trust it
        self.learned.alias(row, real)
        self.say(f"Learned from the tooltip: '{row}' is '{real}'.")
        self.root.after(0, self.rematch)

    def check_selected(self, screen, mon):
        """The row you clicked turns orange (and stays so while 'Are you sure you want to buy
        it?' is up): remember it - when your mesos go down, that is exactly what you bought."""
        for (x, y, w, h), raw, price in getattr(self, "row_spots", []):
            x, y = x - mon["left"] + 50, y - mon["top"]  # (the text only, not the icon)
            px = screen[max(y, 0):y + h, max(x, 0):x + w - 50].astype(np.int16)
            if px.size == 0:
                continue
            b, g, r = px[..., 0], px[..., 1], px[..., 2]
            if ((r > 200) & (g > 120) & (g < 190) & (b < 90)).mean() > 0.4:
                if (raw, price) != getattr(self, "selected", (None, None, 0))[:2]:
                    dlog("selected", read=raw, price=price)
                self.selected = (raw, price, time.time())
                # buying several: 'The total price is 7,670 mesos.' - read it once per box
                box = find_dialog(screen)
                if box and (box, raw) != getattr(self, "total_box", None):
                    total = dialog_total(screen)
                    if total:
                        self.total_box = (box, raw)
                        self.total = (total, time.time())
                        dlog("total", read=raw, price=price, total=total)
                elif not box:
                    self.total_box = None
                return

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
                    stuck = self.incomplete and time.time() - self.incomplete_since > 2.5
                    if (self.layout_try or stuck) and (items or all_grey):
                        # check the fast read once against a full read: a saved layout that is a
                        # bit off reads 1 row of 4 and keeps doing so (Opal Ore never showed up)
                        lines = ocr(img)
                        fresh = find_shop_items(lines)
                        if len(fresh) > len(items) + last_faded:
                            dlog("layout_off", fast=len(items), full=len(fresh), stuck=stuck)
                            if self.layout_try:
                                self.cfg.pop("layout2", None)
                            self.rows = shop_rows(lines)
                            got = read_rows(img, self.rows, seed=True, worth=self.worth)
                            items = got or items
                        self.incomplete_since = time.time()
                    if self.layout_try:
                        self.layout_try = False
                        if not items and not all_grey:  # saved layout doesn't fit this window
                            self.cfg.pop("layout2", None)
                            self.shop_box, self.rows, self.redetect = None, [], True
                            self.busy = False
                            return
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
                    # the text columns + the item icons (the app learns items by their icon)
                    nb = {"left": int(max(x0 - 92, full["left"])), "top": int(max(y0 - 55, full["top"])),
                          "width": 420, "height": int(y1 - y0 + 75)}
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
                            b = self.shop_btn_abs
                            if b and len(self.rows) >= 5:  # a full window: remember its layout
                                self.cfg["layout2"] = {"dx": nb["left"] - b[0], "dy": nb["top"] - b[1],
                                                      "w": nb["width"], "h": nb["height"],
                                                      "rows": [[list(a[:3]) for a in row] for row in self.rows]}
                        elif last_faded:
                            items, rects = [], []  # everything on this page is greyed out
                            self.scanned = sub
                        else:
                            self.rows = []
                    self.shop_box = nb
                    mark_box = nb
                elif box and not items and not auto:
                    self.shop_box = None
                base = self.scanned if mark_box is not None and mark_box is self.shop_box else None
                hidden = 0
                if rects and base is not None and len(rects) == len(items):
                    # rows under the game's tooltip read as garbage ('ourist', 'Orange BaseaRE'): skip them
                    # (only the text: a blue item icon is not a tooltip)
                    keep = [i for i, (x, y, w, h) in enumerate(rects) if not covered(base, (x + 50, y, w - 50, h))]
                    hidden = len(items) - len(keep)
                    items, rects = [items[i] for i in keep], [rects[i] for i in keep]
                # a row hidden (mouse cursor, tooltip): re-read as soon as anything moves
                was = self.incomplete
                self.incomplete = bool(self.rows) and (hidden > 0 or len(items) + last_faded < len(self.rows))
                if self.incomplete and not was:
                    self.incomplete_since = time.time()
                # the list itself is only touched on the window's thread (no races with it)
                took = time.time() - t
                shot = None
                if items and box:
                    sig = tuple(it[:3] for it in items)
                    if sig != getattr(self, "last_shot_sig", None):  # a new page: keep a picture
                        self.last_shot_sig = sig
                        shot = save_shot("page", img)
                dlog("scan", auto=auto, shop=self.cur_shop, ms=int(took * 1000), items=len(items),
                     fast=bool(box and box is self.shop_box and not lines), layout=bool(self.cfg.get("layout2")),
                     shot=shot)
                self.root.after(0, self.apply_scan, items, rects, mark_box, auto, took)
            except Exception as e:
                log_error(type(e), e, e.__traceback__)
                self.say(f"Scan failed: {e}")
            self.busy = False

        threading.Thread(target=work, daemon=True).start()

    def plausible(self, item, qty, got):
        """Could `got` mesos really be what an NPC paid for this? (MeowDB is rarely far off;
        a big difference means something else was sold / gained at the same time)"""
        db = self.prices.get(item)
        if not db:
            return True
        exp = npc_value(item, db, qty)
        return exp * 0.6 <= got <= exp * 1.6

    def lookalike_loses(self, real, qty, price):
        """Is there an item one letter away ('Sai' / 'Saw') that would make this a loss? Then
        a single misread letter decides between +10,000 and -1,000: don't shout, ask to check."""
        cache = self.__dict__.setdefault("_look", {})
        if real not in cache:
            k = norm(real)
            cache[real] = [n for n in self.names if n != real and abs(len(norm(n)) - len(k)) <= 1
                           and Levenshtein.distance(norm(n), k) <= 1] if len(k) <= 12 else []
        for other in cache[real]:
            pays = self.npc_pays(other, qty)
            if pays is not None and pays - price < 0:
                return True
        return False

    def worth(self, name, qty, price):
        """Would this row show up as profitable? (only those get their price double-checked)"""
        real, _ = match_name(name, self.names, self.learned)
        if real and MEM and real in MEM.bought:
            return True  # you buy these: always read them twice
        pays = self.npc_pays(real, qty) if real else None
        return pays is None or pays - price > 0

    def unit_price(self, item):
        return self.learned.prices.get(item, self.prices.get(item))

    def npc_pays(self, item, qty):
        u = self.unit_price(item)
        return None if u is None else npc_value(item, u, qty)

    def apply_scan(self, items, rects, mark_box, auto, took):
        new_good, new_watch, new_cheap, marks, page = False, [], [], [], {}
        market_new = False
        keep = [i for i, it in enumerate(items) if not ignored(it[0])]  # arrows: never worth anything
        if rects:
            rects = [rects[i] for i in keep]
        items = [items[i] for i in keep]
        prev_page, prev_order = self.on_page, getattr(self, "page_order", [])
        if items:
            self.on_page, self.page_order = set(), []
        for i, (name, qty, price, sure) in enumerate(items):
            real, score = match_name(name, self.names, self.learned)
            key = (real or name, price)
            if not real:  # unknown item: a one-letter misread of a row we already have
                key = next((k for k in self.seen if k[1] == price and not self.seen[k][0]
                            and Levenshtein.distance(norm(k[0]), norm(name)) <= 2), key)
            v = (real, name, qty, price)
            pays = self.npc_pays(real, qty) if real else None
            if sure and pays and pays - price >= 1000 and (
                    (pays >= 4 * price and (score < 100 or len(norm(real)) <= 4))
                    or self.lookalike_loses(real, qty, price)):
                # a huge deal on a short / not-quite-matching name: the mouse cursor over 'Saw'
                # made it 'Sai' (13,500). Too good to trust without a look.
                sure = False
                self.odd_name.add(key)
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
            page[key] = page.get(key, 0) + 1
            self.on_page.add(key)
            self.page_order.append(key)
            self.seen_t[key] = time.time()
            watched = self.watched(real or name)
            if watched and (key, self.cur_shop) not in self.watch_seen:
                self.watch_seen.add((key, self.cur_shop))
                new_watch.append(v)
            if sure and self.judge_price(key, v):
                market_new = True
            cheap = sure and not good and self.cheap(v)
            if cheap and (key, self.cur_shop) not in self.cheap_seen:
                self.cheap_seen.add((key, self.cur_shop))
                new_cheap.append(v)
            if rects and (good or watched):
                marks.append((rects[i], p if good else None))
            elif rects and cheap:
                marks.append((rects[i], ("cheap", cheap[1])))
        self.count.update(page)
        for name, qty, price, sure in items:
            real, score = match_name(name, self.names, self.learned)
            key = (real or name, price)
            v = self.seen.get(key)
            dlog("item", shop=self.cur_shop, read=name, item=real, score=score, qty=qty, price=price,
                 sure=bool(sure) and key not in self.unsure, pays=self.npc_pays(real, qty) if real else None,
                 profit=self.profit(v) if v else None, odd=key in self.odd_name, watched=self.watched(real or name))
        if MEM:
            MEM.save()
        if market_new:
            save_json(MARKET_FILE, self.market)
        if items:  # rows that just left the list (a mesos drop may come a moment later)
            van = self.__dict__.setdefault("vanished", {})
            for k in prev_page - self.on_page:
                if k in self.seen:
                    van[k] = (time.time(), self.seen[k], self.cur_shop)
            for k in [k for k, x in van.items() if time.time() - x[0] > 15]:
                del van[k]
        ps = getattr(self, "pending_spend", None)
        if ps and items and (time.time() - ps[1] > 20 or ps[2] != self.cur_shop):
            self.pending_spend = ps = None  # too late / another shop: it was something else
        if ps:
            spent, _, _, before = ps
            gone = [k for k in before - self.on_page if k in self.seen and k not in self.bought_keys
                    and self.seen[k][3] <= spent]
            if len(gone) == 1:  # your mesos went down and exactly one row left the list: that one
                self.pending_spend = None
                k = gone[0]
                if self.seen[k][0]:
                    self.mark_bought(k, self.seen[k], spent)
                else:  # (a row whose name was never read: just stop showing it)
                    self.seen.pop(k, None)
                dlog("bought_by_gone", item=k[0], paid=spent)
        # the same page again with an item missing from the middle: it was bought (by you or
        # someone else). Missing at the top / bottom is just scrolling.
        gone = {k for k in prev_page - self.on_page if k in prev_order[1:-1]}
        if items and gone and len(prev_page & self.on_page) >= max(len(prev_page) - 2, 2):
            for k in gone:
                if k not in self.bought_keys and self.seen_shop.get(k) == self.cur_shop:
                    self.seen.pop(k, None)
                    self.count.pop(k, None)
        if mark_box and rects and len(rects) == len(items):  # where each row is on the screen (tooltips)
            self.row_spots = [((mark_box["left"] + x, mark_box["top"] + y, w, h), it[0], it[2])
                              for (x, y, w, h), it in zip(rects, items)]
        if mark_box and mark_box is self.shop_box:
            self.draw_marks(mark_box, marks)
        profits = [p for _, p in marks if isinstance(p, int)]
        best = max(profits, default=0)
        if new_good:
            self.found_today += sum(profits)
        if new_watch:
            v = new_watch[0]
            self.say(f"👀 WATCHED ITEM: {v[0] or v[1]} for {v[3]:,} at {self.cur_shop or 'this shop'}!")
            if self.beep.get():
                play([1000, 750, 1000, 750], 120)
        elif new_good and self.beep.get():
            if best >= max(10000, 10 * self.min_p()):  # jackpot: a little fanfare
                play([660, 880, 1320], 90)
            else:
                play([880, 1100], 80)
        elif new_cheap and self.beep.get():
            soft_ping()  # just a quiet tik for a cheap market find
        self.show_banner()
        if new_watch or (new_good and best >= max(10000, 10 * self.min_p())):
            self.flash(8 if new_watch else 6)
        self.refresh()
        n_good = sum(1 for k, v in self.seen.items() if (self.profit(v) or 0) > 0
                     and k not in self.unsure and k not in self.bought_keys)
        if items or not auto:
            self.say(("Page changed - " if auto else "") + f"Scan took {took:.1f}s. Found {len(items)} items. "
                     f"Profitable so far: {n_good}.")

    def flash(self, n):
        """Blink the banner (big deal found) so you notice it even looking at the game."""
        bg = self.banner.cget("bg")
        cycle = {"#ffd23f": "#ff3b3b", "#ff3b3b": "#ffd23f", "#9b4dca": "#e056fd", "#e056fd": "#9b4dca"}
        if n <= 0 or bg not in cycle:
            return
        new = cycle[bg]
        self.banner.config(bg=new)
        if self.mini:
            self.mini_deal.config(bg=new)
        self.root.after(250, self.flash, n - 1)

    def set_banner(self, text, bg, fg):
        if text != getattr(self, "last_banner", None):
            self.last_banner = text
            dlog("banner", text=text)
        self.banner.config(text=text, bg=bg, fg=fg)
        if self.mini:
            self.mini_deal.config(text=text, bg=bg, fg=fg)

    def show_banner(self):
        """One big line: what to buy in this shop right now (or: move on)."""
        here = [(self.profit(v), v) for k, v in self.seen.items()
                if self.seen_shop.get(k) == self.cur_shop and k not in self.unsure and k not in self.bought_keys]
        watched = [v for k, v in self.seen.items() if self.seen_shop.get(k) == self.cur_shop
                   and k not in self.bought_keys and self.watched(v[0] or v[1])]
        here = [(p, v) for p, v in here if p is not None and p > 0 and p >= self.min_p()]
        cheap = [(v, self.cheap(v)) for k, v in self.seen.items() if self.seen_shop.get(k) == self.cur_shop
                 and k not in self.unsure and k not in self.bought_keys and self.cheap(v)]
        eye = "   👀 " + ", ".join(f"{v[0] or v[1]} {v[3]:,}" for v in watched[:2]) if watched else ""
        if here:
            # what you can click right now first, then the rest of the shop
            p, v = max(here, key=lambda x: (real_key(x[1]) in self.on_page, x[0]))
            where = ""
            if real_key(v) not in self.on_page:
                t = self.seen_t.get(real_key(v), 0)
                where = (f"  [seen {ago(t)} - may be gone]" if time.time() - t > 120
                         else "  [other page - scroll]")
            n = self.count.get(real_key(v), 1)
            total = sum(x[0] * self.count.get(real_key(x[1]), 1) for x in here)
            rows = sum(self.count.get(real_key(x[1]), 1) for x in here)
            more = f"   ({rows} to buy, total +{total:,})" if rows > 1 else ""
            hot = p >= max(10000, 10 * self.min_p())
            times = f" ×{n}" if n > 1 else ""
            self.set_banner(f"{'💰' if hot else '✅'} BUY: {v[0]}{times}  for {v[3]:,}  →  +{p:,}{where}{more}{eye}",
                            "#ffd23f" if hot else "#2fb344", "#000" if hot else "#fff")
        elif cheap:
            v, (unit, low) = cheap[0]
            more = f"  (+{len(cheap) - 1} more)" if len(cheap) > 1 else ""
            self.set_banner(f"🔵 CHEAP: {v[0]}  for {v[3]:,}  (lowest seen {low:,}){more}{eye}", "#2b8cff", "#fff")
        elif watched:
            v = watched[0]
            self.set_banner(f"👀 WATCHED: {v[0] or v[1]}  for {v[3]:,}" + (f"  (+{len(watched) - 1} more)"
                            if len(watched) > 1 else ""), "#9b4dca", "#fff")
        elif self.cur_shop:
            again = f"  (checked {ago(self.revisit[0])})" if self.revisit else ""
            self.set_banner(f"✖ Nothing at {self.cur_shop}{again} - next shop!", "#e8e8e8", "#777")
        self.shops_lbl.config(text=f"Shops checked this session: {self.shops_today}    "
                                   f"Profit spotted: +{self.found_today:,}")

    def watched(self, name):
        n = norm(name or "")
        return bool(n) and any(w and norm(w) in n for w in self.watch)

    def watch_dialog(self):
        top = tk.Toplevel(self.root)
        top.title("Watch list")
        top.attributes("-topmost", True)
        ttk.Label(top, text="Items to always tell you about (one per line, part of the name is enough).\n"
                            "Purple frame + sound when one shows up in a shop - even if it isn't profitable.",
                  foreground="#555").pack(padx=10, pady=(8, 4), anchor="w")
        txt = tk.Text(top, width=40, height=12, font=("Segoe UI", 10))
        txt.insert("1.0", "\n".join(self.watch))
        txt.pack(padx=10, fill="both", expand=True)
        txt.focus_set()

        def save():
            self.watch = [w.strip() for w in txt.get("1.0", "end").splitlines() if w.strip()]
            self.cfg["watch"] = self.watch
            save_json(CONFIG_FILE, self.cfg)
            self.watch_seen.clear()
            top.destroy()
            self.refresh()
            self.show_banner()
            self.say(f"Watching {len(self.watch)} items.")
        ttk.Button(top, text="Save", command=save).pack(pady=8)

    def watch_item(self, item):
        if item and item not in self.watch:
            self.watch.append(item)
            self.cfg["watch"] = self.watch
            save_json(CONFIG_FILE, self.cfg)
            self.refresh()
            self.say(f"👀 Watching '{item}' from now on.")

    def unwatch(self, item):
        n = norm(item)
        self.watch = [w for w in self.watch if not (w and norm(w) in n)]
        self.cfg["watch"] = self.watch
        save_json(CONFIG_FILE, self.cfg)
        self.refresh()
        self.show_banner()
        self.say(f"Stopped watching '{item}'.")

    def complain(self):
        """F11: something looks wrong right now - keep a full screenshot and mark the log, so
        Claude can see exactly this moment."""
        try:
            shot = save_shot("F11", grab()[:, :, :3], keep=400)
        except Exception:
            shot = None
        dlog("F11", shot=shot, banner=self.banner.cget("text"), shop=self.cur_shop,
             rows=[self.tree.item(i, "values") for i in self.tree.get_children()][:40])
        self.say(f"📌 Saved this moment ({time.strftime('%H:%M:%S')}) - tell Claude what was wrong.")

    def debug_zip(self):
        """Pack the debug log + pictures into one zip on the Desktop, ready to send."""
        import zipfile
        desk = os.path.join(os.path.expanduser("~"), "Desktop")
        out = os.path.join(desk if os.path.isdir(desk) else DATA, time.strftime("MapleFlipper-debug-%m%d-%H%M.zip"))
        if MEM:
            MEM.save(force=True)
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            for folder, _, files in os.walk(DEBUG_DIR):
                for fn in files:
                    p = os.path.join(folder, fn)
                    z.write(p, os.path.relpath(p, DATA))
            for fn in ("error_log.txt", "config.json", "learned.json", "flips.json"):
                p = os.path.join(DATA, fn)
                if os.path.exists(p):
                    z.write(p, fn)
        self.say(f"Debug zip saved: {out}")
        try:
            os.startfile(os.path.dirname(out))  # Windows: show it
        except Exception:
            pass

    def quit_requested(self):
        """A newer copy was started: close (saving everything) so it can take over."""
        try:
            if int(open(QUIT_FILE).read().strip()) == os.getpid():
                dlog("quit_for_new_copy")
                self.on_close()
                return
        except (OSError, ValueError):
            pass
        self.root.after(1000, self.quit_requested)

    def restart(self):
        """Stuck or acting weird: start the app again (keeps everything it learned)."""
        import subprocess
        self.on_close_save()
        args = [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, os.path.abspath(__file__)]
        subprocess.Popen(args)
        os._exit(0)  # don't wait for the background threads

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
            watched = self.watched(real or raw)
            n = self.count.get(key, 1)
            times = f" ×{n}" if n > 1 else ""
            if (self.only_profit.get() and not bought and not unsure and not watched
                    and (p is None or p <= 0 or p < self.min_p())):
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
                odd = key in self.odd_name
                vals, tag = (f"{real or raw} ({'name' if odd else 'price'}?)", qty, f"{price:,}" + ("" if odd else " ?"),
                             "-" if pays is None else f"{pays:,}", "check name" if odd else "check price", shop), "unsure"
            elif p is None and (self.is_nx(real) or self.cheap(v) or self.market_range(real)):
                ch = self.cheap(v)
                rng = self.market_range(real)
                vals = (f"{'💎 ' if self.is_nx(real) else ''}{real}{times}", qty, f"{price:,}",
                        "NX - no NPC" if self.is_nx(real) else "unknown",
                        f"low {rng[0]:,} ({rng[2]})" if rng else "-", shop)
                tag = "cheap" if ch else "nx"
            elif p is None:
                vals, tag = (f"{raw} (?)", qty, f"{price:,}", "unknown", "-", shop), "unknown"
            else:
                mark = " ✓" if real in self.learned.prices else ""
                prof = f"{p:+,}" + (f" ×{n}" if n > 1 and p > 0 else "")
                vals = (real + times, qty, f"{price:,}", f"{self.npc_pays(real, qty):,}{mark}", prof, shop)
                tag = "good" if p > 0 and p >= self.min_p() else "cheap" if self.cheap(v) else "bad"
            if watched and tag != "bought":
                vals = ("👀 " + vals[0],) + vals[1:]
                if tag != "good":
                    tag = "watch"
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
        if real and real_key(v) not in self.bought_keys:
            m.add_command(label="✔ I already bought this", command=lambda: self.mark_bought(real_key(v), v, price))
        if self.watched(real or raw):
            m.add_command(label=f"✖ Stop watching '{real or raw}'", command=lambda: self.unwatch(real or raw))
        else:
            m.add_command(label=f"👀 Watch '{real or raw}'", command=lambda: self.watch_item(real or raw))
        if real:
            m.add_command(label=f"Set real NPC price of '{real}'...", command=lambda: self.ask_price(real))
            if self.is_nx(real):
                m.add_command(label=f"'{real}' is NOT an NX item",
                              command=lambda: (self.learned.nx.discard(real), self.learned.save(), self.refresh()))
            else:
                m.add_command(label=f"💎 '{real}' is an NX item (NPCs don't buy it)",
                              command=lambda: (self.learned.mark_nx(real), self.refresh()))
            m.add_command(label=f"Set market price of '{real}' by hand...", command=lambda: self.ask_market(real))
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
            self.learned.manual.add(item)
            self.learned.set_price(item, val)
            self.say(f"Learned: NPC pays {val:,} for '{item}'.")
            self.refresh()

    def ask_market(self, item):
        from tkinter import simpledialog
        rng = self.market_range(item)
        val = simpledialog.askstring("Typical market price", f"Typical player price of ONE '{item}'\n"
                                     "(low-high, e.g. 40000-60000; empty = forget):", parent=self.root,
                                     initialvalue=f"{rng[0]}-{rng[1]}" if rng else "")
        if val is None:
            return
        nums = [to_int(x) for x in re.split(r"[-–~ ]+", val.replace(",", "")) if to_int(x)]
        if nums:
            self.learned.market[item] = [min(nums), max(nums)]
        else:
            self.learned.market.pop(item, None)
        self.learned.save()
        self.refresh()
        self.show_banner()

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

    def mark_bought(self, key, v, spent):
        """You bought this row (mesos went down by `spent`): stop suggesting it, wait for the NPC sale."""
        real, raw, qty, price = v
        p = self.profit((real, raw, qty, spent))
        if (p is None or p <= 0) and not self.watched(real):
            # bought for yourself (no profit selling it to an NPC): not a flip, don't track it
            dlog("bought_not_flip", item=real, paid=spent, profit=p)
            self.seen.pop(key, None)
            self.root.after(0, self.refresh)
            return
        if price != spent:  # show what you really paid
            self.seen.pop(key, None)
            self.unsure.discard(key)
            v = (real, raw, qty, spent)
            key = real_key(v)
            self.seen[key] = v
            self.seen_shop.setdefault(key, self.cur_shop)
        self.seen.setdefault(key, v)  # (a row that already left the list: show it as bought)
        self.bought_keys[key] = spent
        dlog("bought", item=real, read=raw, shop_price=price, paid=spent)
        if MEM:
            MEM.bought[real] = MEM.bought.get(real, 0) + 1
            MEM.dirty = True
        if self.seen_shop.get(key):
            self.stats.setdefault("from", {})[real] = self.seen_shop[key]
        pays = self.npc_pays(real, qty)
        self.bought.append([real, raw, qty, pays, spent])
        self.save_stats()
        self.root.after(0, self.refresh)
        self.root.after(0, self.show_banner)
        res = "" if pays is None else f" NPC pays {pays:,} -> {pays - spent:+,}."
        fix = (f" ({qty} × {price // max(qty, 1):,})" if price and spent % price == 0 and qty > 1
               else f" (the shop read {price:,} - fixed)" if price != spent else "")
        self.say(f"✔ BOUGHT '{real}' for {spent:,}{fix}.{res} Now sell it to an NPC.")

    def learn_from_mesos(self, diff):
        """Buying from a shop and selling to an NPC show up as mesos changes: use them to check
        and correct the prices automatically."""
        if diff < 0:
            spent = -diff
            raw, sel_price, t = getattr(self, "selected", (None, None, 0))
            if raw and time.time() - t < 30:
                # the row you selected (orange) before the mesos went down: that's the purchase.
                # The price only has to fit loosely (a misread digit / several bundles at once)
                hit = [(k, v) for k, v in self.seen.items() if v[1] == raw and v[3] == sel_price
                       and k not in self.bought_keys]
                fits = sel_price and (spent % sel_price == 0 and spent // sel_price <= 20
                                      or lost_digits(str(sel_price), str(spent))
                                      or abs(spent - sel_price) <= 0.1 * sel_price)
                total, tt = getattr(self, "total", (None, 0))
                exact = total == spent and time.time() - tt < 30  # the box said so: certain
                if hit and (fits or exact) and hit[0][1][0]:
                    self.selected, self.total = (None, None, 0), (None, 0)
                    key, v = hit[0]
                    n = spent // sel_price if spent % sel_price == 0 else 1  # how many you bought
                    dlog("bought_selected", read=raw, price=sel_price, paid=spent, n=n, sure=exact)
                    self.mark_bought(key, (v[0], v[1], v[2] * n, v[3]), spent)
                    return
            cands = [(k, v) for k, v in self.seen.items() if v[0] and k not in self.bought_keys]
            hit = [kv for kv in cands if kv[1][3] == spent]
            if not hit:  # bought 2+ of the same bundle at once
                hit = [kv for kv in cands if kv[1][3] and spent % kv[1][3] == 0 and spent // kv[1][3] <= 20]
            if not hit:  # the shop price was misread (a digit lost: 2,999 read as 299)?
                hit = [kv for kv in cands if lost_digits(str(kv[1][3]), str(spent))]
            if len(hit) != 1 and not any(kv[1][3] == spent for kv in hit):
                dlog("spent_unmatched", spent=spent, candidates=[kv[1][0] for kv in hit])
                van = [(k, x[1]) for k, x in getattr(self, "vanished", {}).items()
                       if time.time() - x[0] < 10 and x[2] == self.cur_shop and k not in self.bought_keys
                       and x[1][0] and x[1][3] <= spent]
                if len(van) == 1:  # a row left the list just before the mesos went down: that one
                    self.vanished.pop(van[0][0], None)
                    self.mark_bought(van[0][0], van[0][1], spent)
                    return
                # the price didn't say which row it was: the row that disappears from the shop
                # list in the next seconds will (see apply_scan). Re-read the list right away.
                self.pending_spend = (spent, time.time(), self.cur_shop, set(self.on_page))
                self.scanned = None
                return
            key, v = next((kv for kv in hit if kv[1][3] == spent), hit[0])
            self.mark_bought(key, v, spent)
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
        if combos:
            # several sets add up: any of them is the same profit, take the one with the most items
            # (selling everything you carry at once is the usual case). Never ask.
            self.sold_many(max(combos, key=len))
        elif len(self.bought) == 1 and self.plausible(self.bought[0][0], self.bought[0][2], diff):
            self.apply_sale(0, diff)
        else:
            # a gain that has nothing to do with the flips (a trade, a quest, other items sold)
            dlog("gain_unmatched", gain=diff, waiting=[b[0] for b in self.bought])

    def sold(self, item):
        """Sold to the NPC: the 'BOUGHT' row is done."""
        for k in [k for k in self.bought_keys if k[0] == item][:1]:
            self.bought_keys.pop(k)
            self.seen.pop(k, None)

    def save_stats(self):
        save_json(STATS_FILE, self.stats)
        self.root.after(0, self.show_stats)

    def record_flip(self, item, paid, got):
        seller = self.stats.setdefault("from", {}).pop(item, "")
        dlog("flip", item=item, paid=paid, got=got)
        self.stats["flips"].append({"time": time.strftime("%Y-%m-%d %H:%M"), "item": item,
                                    "paid": paid, "got": got, "shop": seller})
        self.session_flips += got - paid
        if self.track_t0:
            self.track_flips += got - paid
        self.save_stats()

    def stats_window(self):
        """Your flips: today / this week / all time, best items and best sellers."""
        flips = self.stats["flips"]
        top = tk.Toplevel(self.root)
        top.title("Your flips")
        top.attributes("-topmost", True)
        nb = ttk.Notebook(top)
        nb.pack(fill="both", expand=True, padx=8, pady=8)
        today = time.strftime("%Y-%m-%d")
        week = time.strftime("%Y-%m-%d", time.localtime(time.time() - 6 * 86400))
        for title, fs in (("Today", [f for f in flips if f["time"] >= today]),
                          ("7 days", [f for f in flips if f["time"] >= week]), ("All time", flips)):
            pg = ttk.Frame(nb)
            nb.add(pg, text=title)
            prof = sum(f["got"] - f["paid"] for f in fs)
            best = max(fs, key=lambda f: f["got"] - f["paid"], default=None)
            ttk.Label(pg, text=f"{prof:+,} mesos", font=("Segoe UI", 20, "bold"),
                      foreground="#1a8f3a" if prof >= 0 else "#c01c28").pack(anchor="w", padx=8, pady=(8, 0))
            line = f"{len(fs)} flips" + (f"   ·   {prof // len(fs):+,} per flip" if fs else "")
            if best:
                line += f"\nBest: {best['item']} {best['got'] - best['paid']:+,}  ({best['time']})"
            ttk.Label(pg, text=line, foreground="#555").pack(anchor="w", padx=8)
            for what, col, key in (("Best items", "Item", "item"), ("Best sellers", "Seller", "shop")):
                sums = {}
                for f in fs:
                    if f.get(key):
                        n, p = sums.get(f[key], (0, 0))
                        sums[f[key]] = (n + 1, p + f["got"] - f["paid"])
                ttk.Label(pg, text=what, font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=8, pady=(10, 2))
                tv = ttk.Treeview(pg, columns=("n", "x", "p"), show="headings", height=6)
                for c, t, w in (("n", col, 170), ("x", "Flips", 60), ("p", "Profit", 90)):
                    tv.heading(c, text=t)
                    tv.column(c, width=w, anchor="w" if c == "n" else "e")
                for name, (n, p) in sorted(sums.items(), key=lambda kv: -kv[1][1])[:10]:
                    tv.insert("", "end", values=(name, n, f"{p:+,}"))
                tv.pack(fill="x", padx=8)
        ttk.Button(top, text="Close", command=top.destroy).pack(pady=(0, 8))

    def show_stats(self):
        today = time.strftime("%Y-%m-%d")
        flips = self.stats["flips"]
        t = [f for f in flips if f["time"].startswith(today)]
        prof = lambda fs: sum(f["got"] - f["paid"] for f in fs)
        self.stats_lbl.config(text=f"Flips today: {len(t)}  profit {prof(t):+,}    "
                                   f"All time: {len(flips)}  profit {prof(flips):+,}    (click for stats)")
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
            db = self.prices.get(real)
            if db and not db / 3 <= unit <= db * 3 and real not in self.learned.manual:
                continue  # a misread digit on the NPC shop, not a real price
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
        if not self.plausible(item, qty, got):  # don't learn nonsense: just count the flip
            self.record_flip(item, paid, expect if expect is not None else got)
            self.sold(item)
            self.say(f"Sold '{item}' - got {got:,} but it should pay ~{expect:,}; "
                     f"price NOT changed (right-click the row to set it yourself).")
            self.refresh()
            return
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
        pad = 70  # (the overlay starts a bit left of the watched area)
        w = box["width"] + pad + 140
        self.overlay.geometry(f"{w}x{box['height']}+{box['left'] - pad}+{box['top']}")
        self.overlay.deiconify()
        c = self.ov_canvas
        c.delete("all")
        best = max((p for _, p in marks if isinstance(p, int)), default=None)
        for (x, y, rw, rh), p in marks:
            x += pad
            if isinstance(p, tuple):  # under 80% of the lowest price seen lately
                c.create_rectangle(x, y, x + rw, y + rh, outline="#2b8cff", width=3)
                c.create_text(x + rw + 6, y + rh / 2, anchor="w", text=f"CHEAP (low {p[1]:,})",
                              fill="#2b8cff", font=("Segoe UI", 11, "bold"))
                continue
            if p is None:  # an item on your watch list
                c.create_rectangle(x, y, x + rw, y + rh, outline="#b44dff", width=4)
                c.create_text(x + rw + 6, y + rh / 2, anchor="w", text="👀 WATCH",
                              fill="#b44dff", font=("Segoe UI", 12, "bold"))
                continue
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
        self.on_close_save()
        self.root.destroy()

    def on_close_save(self):
        self.cfg.update(tracking=self.tracking, marks=self.show_marks.get(),
                        geometry=self.root.geometry(), min_profit=self.min_p(), beep=self.beep.get(), volume=self.volume.get(),
                        auto=self.auto.get(), mini=self.mini_on)
        self.save_shop()
        if MEM:
            MEM.save(force=True)
        dlog("close")
        if self.mini:
            self.cfg["mini_pos"] = [self.mini.winfo_x(), self.mini.winfo_y()]
        save_json(CONFIG_FILE, self.cfg)

    def left_shop(self):
        """Closed a shop with a good deal still in it: say so - loudly if it was a big one."""
        shop = self.cur_shop
        if not shop or getattr(self, "warned_shop", None) == (shop, self.shops_today):
            return False
        self.warned_shop = (shop, self.shops_today)
        deals = [(self.profit(v), v) for k, v in self.seen.items() if self.seen_shop.get(k) == shop
                 and k not in self.unsure and k not in self.bought_keys]
        deals = [(p, v) for p, v in deals if p is not None and p >= max(1000, 5 * self.min_p())]
        if not deals:
            return False
        p, v = max(deals, key=lambda x: x[0])
        dlog("missed", shop=shop, item=v[0], price=v[3], profit=p, n=len(deals))
        self.say(f"⚠ You left {shop} without buying {v[0]} (+{p:,})!")
        self.set_banner(f"⚠ MISSED at {shop}: {v[0]} for {v[3]:,} → +{p:,}" +
                        (f"  (+{len(deals) - 1} more)" if len(deals) > 1 else "") + " - go back?", "#ff3b3b", "#fff")
        if self.beep.get():
            play([880, 660, 440], 140)
        return True

    def new_shop(self, seller):
        """Another shop: drop the losers of the previous ones, keep the profitable items
        until they are bought."""
        missed = self.left_shop()
        dlog("shop", seller=seller, revisit=bool(self.known_shop(seller)))
        for key, v in list(self.seen.items()):
            p = self.profit(v)
            if (p is None or p <= 0 or p < self.min_p() or key in self.unsure) and key not in self.bought_keys:
                del self.seen[key]
                self.unsure.discard(key)
        self.save_shop()  # the shop you just left
        self.cur_shop = seller
        self.on_page, self.page_order = set(), []
        self.shops_today += 1
        name = self.known_shop(seller)
        rec = self.shops.get(name) if name else None
        self.revisit = None
        if rec and time.time() - rec["t"] < 12 * 3600:
            self.revisit = (rec["t"], rec["best"])
            self.cur_shop = name  # same spelling as last time
            best = f"best was +{rec['best']:,}" if rec["best"] > 0 else "nothing worth buying"
            self.say(f"↺ {name}: you checked this shop {ago(rec['t'])} - {best}.")
        self.refresh()
        if self.revisit:
            self.set_banner(f"↺ {self.cur_shop}: checked {ago(self.revisit[0])} - "
                            + (f"best was +{self.revisit[1]:,}" if self.revisit[1] else "nothing then"),
                            "#5b6abf", "#fff")
        elif not missed:  # (keep the 'MISSED' warning up until this shop has been read)
            self.set_banner(f"🔍 Reading {seller or 'the shop'}...", "#e8e8e8", "#555")
        self.shops_lbl.config(text=f"Shops checked this session: {self.shops_today}    "
                                   f"Profit spotted: +{self.found_today:,}")

    def known_shop(self, seller):
        if not seller:
            return None
        if seller in self.shops:
            return seller
        return next((n for n in self.shops if same_seller(n, seller)), None)

    def save_shop(self):
        """Remember what the current shop had (best profit right now), for the next visit."""
        if not self.cur_shop:
            return
        best = max([p for k, v in self.seen.items() if self.seen_shop.get(k) == self.cur_shop
                    and k not in self.unsure and (p := self.profit(v)) is not None and p > 0] or [0])
        self.shops[self.cur_shop] = {"t": time.time(), "best": best}
        if len(self.shops) > 500:  # keep the newest
            self.shops = dict(sorted(self.shops.items(), key=lambda kv: -kv[1]["t"])[:400])
        save_json(SHOPS_FILE, self.shops)

    def shops_window(self):
        top = tk.Toplevel(self.root)
        top.title("Shops you checked")
        top.attributes("-topmost", True)
        tv = ttk.Treeview(top, columns=("shop", "when", "best"), show="headings", height=16)
        for c, t, w in (("shop", "Seller", 170), ("when", "Checked", 110), ("best", "Best deal", 100)):
            tv.heading(c, text=t)
            tv.column(c, width=w, anchor="w" if c == "shop" else "e")
        tv.tag_configure("good", background="#b8f5b0")
        tv.tag_configure("old", foreground="#999")
        for name, rec in sorted(self.shops.items(), key=lambda kv: -kv[1]["t"])[:200]:
            old = time.time() - rec["t"] > 12 * 3600
            tv.insert("", "end", values=(name, ago(rec["t"]), f"+{rec['best']:,}" if rec["best"] else "nothing"),
                      tags=("old",) if old else ("good",) if rec["best"] else ())
        tv.pack(fill="both", expand=True, padx=8, pady=8)
        ttk.Button(top, text="Forget all", command=lambda: (self.shops.clear(), save_json(SHOPS_FILE, {}),
                                                            top.destroy())).pack(pady=(0, 8))

    def clear(self):
        self.count.clear()
        self.watch_seen.clear()
        self.hide_marks()
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
            self.track_t0, self.track_flips = time.time(), 0
            dlog("track_start")
            self.tick()
            threading.Thread(target=self.track_loop, daemon=True).start()
        elif self.track_t0:
            spent = time.time() - self.track_t0
            self.say(f"⏱ Tracked {clock(spent)} - flips {self.track_flips:+,} ({self.per_hour():+,}/h)")
            dlog("track_stop", seconds=int(spent), flips=self.track_flips)
            self.track_t0 = None
            self.timer_lbl.config(text="")
            self.update_mini_mesos()

    def per_hour(self):
        hours = max((time.time() - self.track_t0) / 3600, 0.25) if self.track_t0 else 1
        return round(self.track_flips / hours)

    def tick(self):
        """The tracking timer: once a second, on the window and the mini bar."""
        if not self.track_t0:
            return
        txt = f"⏱ {clock(time.time() - self.track_t0)}"
        if self.track_flips:
            txt += f"   {self.track_flips:+,} ({self.per_hour():+,}/h)"
        self.timer_lbl.config(text=txt, foreground="#1a8f3a" if self.track_flips >= 0 else "#c01c28")
        if self.mini:
            self.mini_timer.config(text=f"⏱ {clock(time.time() - self.track_t0)}")
        self.root.after(1000, self.tick)

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
            if v is not None and time.time() - getattr(self, "shop_mesos_t", 0) < 3:
                v = None  # in a shop: its own mesos box is read instead (two sources would fight)
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
            dlog("mesos", old=old, new=v, diff=diff)
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
        tk.Button(top, text="⟳", command=self.restart, **btn).pack(side="right", padx=2, pady=3)
        tk.Button(top, text="🔍 Scan", command=self.scan, **btn).pack(side="right", padx=2, pady=3)
        self.mini_mesos = tk.Label(top, text="Mesos: -", bg="#1f1f24", fg="#ddd", font=("Segoe UI", 10, "bold"))
        self.mini_mesos.pack(side="left", padx=4)
        self.mini_flips = tk.Label(top, text="", bg="#1f1f24", fg="#5ee07a", font=("Segoe UI", 10, "bold"))
        self.mini_flips.pack(side="left", padx=4)
        self.mini_timer = tk.Label(top, text="", bg="#1f1f24", fg="#7fb8ff", font=("Consolas", 10, "bold"))
        self.mini_timer.pack(side="left", padx=4)
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
            mm.add_command(label="Clear list (F10)", command=self.clear)
            mm.add_command(label="Mark items as sold...", command=self.sell_dialog)
            mm.add_command(label="Shops you checked...", command=self.shops_window)
            mm.add_command(label="Your flips (stats)...", command=self.stats_window)
            mm.add_command(label="👀 Watch list...", command=self.watch_dialog)
            mm.add_command(label="⟳ Restart the app", command=self.restart)
            mm.add_command(label="⬆ Check for update", command=lambda: self.check_update(manual=True))
            mm.add_command(label="Save debug zip (for Claude)", command=self.debug_zip)
            mm.add_separator()
            mm.add_command(label="Quit", command=self.on_close)
            mm.tk_popup(e.x_root, e.y_root)

        for w in (m, grip, top, self.mini_mesos, self.mini_flips, self.mini_timer, self.mini_deal, self.mini_status,
                  self.mini_sell):
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
            self.mini_mesos.config(text=f"{self.mesos:,}", fg="#ddd")
        if not self.track_t0:
            self.mini_timer.config(text="")
        hours = max((time.time() - (self.track_t0 or self.session_t)) / 3600, 0.25)  # no silly numbers at first
        p = self.track_flips if self.track_t0 else self.session_flips
        self.mini_flips.config(text=f"flips {p:+,}  ({round(p / hours):+,}/h)" if p else "",
                               fg="#5ee07a" if p >= 0 else "#ff6b6b")
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


PID_FILE = os.path.join(DATA, "running.pid")
QUIT_FILE = os.path.join(DATA, "quit.request")


def our_process_alive(pid):
    """Is `pid` a running MapleFlipper (python / MapleFlipper.exe)? Checks the program name too,
    so an unrelated program that got the same number is never touched."""
    try:
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            k.GetExitCodeProcess(h, ctypes.byref(code))
            buf, size = ctypes.create_unicode_buffer(1024), ctypes.c_ulong(1024)
            k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
            name = os.path.basename(buf.value).lower()
            return code.value == 259 and ("python" in name or "mapleflipper" in name)  # STILL_ACTIVE
        finally:
            k.CloseHandle(h)
    except Exception:
        return False


def close_old_copy():
    """Started again (START.bat / the exe) while an older copy still runs: ask it to close
    (it saves everything first), and if it doesn't within 5 s, end it. Two copies would fight
    over the screen, the hotkeys and the files."""
    try:
        old = int(open(PID_FILE).read().strip())
    except (OSError, ValueError):
        old = None
    if old and old != os.getpid() and our_process_alive(old):
        with open(QUIT_FILE, "w") as f:
            f.write(str(old))
        for _ in range(50):
            if not our_process_alive(old):
                break
            time.sleep(0.1)
        else:
            os.system(f"taskkill /PID {old} /F >nul 2>&1")
        dlog("closed_old_copy", pid=old)
    try:
        os.remove(QUIT_FILE)
    except OSError:
        pass
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--build-prices":  # used by the release build
        print(update_prices(lambda t, p=None: print(t, flush=True), path=sys.argv[2]))
    else:
        close_old_copy()
        App().run()
