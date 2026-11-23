"""One search across everything, ranked the way the household wants:

Tier 1  We own it      (this library, Audiobookshelf, imported Amazon/Audible purchases)
Tier 2  Public domain  (Project Gutenberg ebooks, LibriVox/Internet Archive audiobooks)
Tier 3  Library where we have a card
Tier 4  Library where we could get a free card
Tier 5  Library where we could buy a card
Tier 6  Buy it (Amazon / Audible / Libro.fm links; no prices, Amazon doesn't allow it)

Libby availability needs OverDrive's catalog. OverDrive only offers its official API to
libraries, so live availability is an opt-in setting (off by default) that reads the same
public catalog the Libby website reads. With it off, the app shows "Check in Libby" links.
"""

import csv
import io
import json
import math
import re
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote

import httpx

from . import audio, db
from .config import GUTENDEX, USER_AGENT, env
from .library import flip_name

THUNDER = env("OVERDRIVE_URL", "https://thunder.api.overdrive.com/v2")
HEADERS = {"User-Agent": USER_AGENT}
TIER_LABEL = {1: "You own it", 2: "Free (public domain)", 3: "Library (your card)",
              4: "Library (free card)", 5: "Library (paid card)", 6: "Buy"}
KIND_RANK = {"card": 3, "free": 4, "paid": 5}

_pool = ThreadPoolExecutor(max_workers=8)
_libby_gate = threading.Semaphore(3)  # never more than 3 OverDrive requests at once
_cache: dict = {}
_cache_lock = threading.Lock()


def cached(key, ttl, fn):
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    val = fn()
    with _cache_lock:
        _cache[key] = (now, val)
        if len(_cache) > 500:  # keep memory small
            for k in sorted(_cache, key=lambda k: _cache[k][0])[:100]:
                _cache.pop(k, None)
    return val


# ---------------------------------------------------------------- settings & tables

def migrate():
    db.x("""CREATE TABLE IF NOT EXISTS libraries (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, libby_key TEXT, kind TEXT,
        fee TEXT, note TEXT, url TEXT, created REAL)""")
    db.x("""CREATE TABLE IF NOT EXISTS owned_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT, kind TEXT, title TEXT,
        author TEXT, norm TEXT, created REAL)""")
    db.x("CREATE INDEX IF NOT EXISTS owned_norm ON owned_items(norm)")


def prefs():
    return {
        "libby_lookup": db.get_setting("libby_lookup", "0") == "1",
        "wait_ok_days": int(db.get_setting("wait_ok_days", "42")),
        "home_state": db.get_setting("home_state", ""),
    }


def libraries():
    rows = db.q("SELECT * FROM libraries")
    return sorted(rows, key=lambda r: (KIND_RANK.get(r["kind"], 9), r["name"] or ""))


def parse_libby_key(text):
    """Accept a libbyapp.com link, an OverDrive site address, or the bare key."""
    t = (text or "").strip()
    m = re.search(r"libbyapp\.com/(?:library|search|shelf|interview/[^/]+)/([A-Za-z0-9-]+)", t)
    if m:
        return m.group(1).lower()
    m = re.match(r"(?:https?://)?([A-Za-z0-9-]+)\.overdrive\.com", t)
    if m and m.group(1).lower() not in ("www", "app", "libbyapp", "help", "company"):
        return m.group(1).lower()
    if re.fullmatch(r"[A-Za-z0-9-]{2,40}", t):
        return t.lower()
    return None


def verify_libby_key(key):
    """Only used when the opt-in lookup is on. Returns the library's name or None."""
    with _libby_gate:
        r = httpx.get(f"{THUNDER}/libraries/{key}", headers=HEADERS, timeout=15)
    if r.status_code != 200:
        return None
    data = r.json()
    return data.get("name") or data.get("libraryName") or key


def libby_search_url(key, q):
    return f"https://libbyapp.com/search/{key}/search/query-{quote(q, safe='')}/page-1"


def buy_links(q, kind=None):
    links = []
    if kind in (None, "ebook"):
        links.append({"label": "Kindle Store", "url": f"https://www.amazon.com/s?k={quote(q)}&i=digital-text"})
    if kind in (None, "audiobook"):
        links.append({"label": "Libro.fm (DRM-free)", "url": f"https://libro.fm/search?q={quote(q)}"})
        links.append({"label": "Audible", "url": f"https://www.audible.com/search?keywords={quote(q)}"})
    return links


# ---------------------------------------------------------------- matching helpers

def norm_title(t):
    t = (t or "").lower()
    t = re.sub(r"\(.*?\)|\[.*?\]", " ", t)
    t = re.split(r"[:;]| - | — ", t)[0]
    t = re.sub(r"[^\w\s]", " ", t)
    t = re.sub(r"^(the|a|an)\s+", "", t.strip())
    return re.sub(r"\s+", " ", t).strip()


def author_key(a):
    a = flip_name((a or "").split("&")[0].split(";")[0].strip())
    words = re.findall(r"[a-zA-Z]+", a or "")
    return words[-1].lower() if words else ""


def relevance(q, title, author):
    qt = set(re.findall(r"\w+", q.lower())) - {"the", "a", "an", "of", "and", "by"}
    if not qt:
        return 0
    hay = set(re.findall(r"\w+", f"{title} {author}".lower()))
    return len(qt & hay) / len(qt)


# ---------------------------------------------------------------- sources

def src_local(q, user):
    like = f"%{q.strip()}%"
    rows = db.q("""SELECT id, title, author FROM books WHERE status='done'
                   AND (title LIKE ? OR author LIKE ? OR series LIKE ?) LIMIT 25""", (like, like, like))
    words = [w for w in re.findall(r"\w+", q.lower()) if len(w) > 2]
    if not rows and words:  # all words anywhere in title+author
        cond = " AND ".join(["(LOWER(title)||' '||LOWER(COALESCE(author,''))) LIKE ?"] * len(words))
        rows = db.q(f"SELECT id, title, author FROM books WHERE status='done' AND {cond} LIMIT 25",
                    tuple(f"%{w}%" for w in words))
    return [{"title": r["title"], "author": r["author"], "kind": "ebook", "tier": 1, "status": "owned",
             "source": "Our library", "action": {"type": "book", "id": r["id"]}} for r in rows]


def src_owned_imports(q, user):
    words = [w for w in re.findall(r"\w+", q.lower()) if len(w) > 1]
    if not words:
        return []
    cond = " AND ".join(["(LOWER(title)||' '||LOWER(COALESCE(author,''))) LIKE ?"] * len(words))
    rows = db.q(f"SELECT * FROM owned_items WHERE {cond} LIMIT 25", tuple(f"%{w}%" for w in words))
    out = []
    for r in rows:
        if r["source"] == "audible":
            label, url = "Audible purchase", "https://www.audible.com/library/titles"
        elif r["source"] == "kindle":
            label, url = "Kindle purchase", "https://www.amazon.com/hz/mycd/digital-console/contentlist/booksAll/dateDsc/"
        else:
            label, url = "Owned elsewhere", None
        out.append({"title": r["title"], "author": r["author"], "kind": r["kind"], "tier": 1,
                    "status": "owned", "source": label, "action": {"type": "link", "url": url} if url else None})
    return out


def src_audiobookshelf(q, user):
    s = audio.settings()
    if not (s["abs_url"] and s["abs_token"]):
        return []
    base = s["abs_url"].rstrip("/")
    h = {"Authorization": f"Bearer {s['abs_token']}", **HEADERS}

    def fetch():
        libs = httpx.get(f"{base}/api/libraries", headers=h, timeout=10).json().get("libraries", [])
        found = []
        for lib in libs:
            if lib.get("mediaType", "book") != "book":
                continue
            r = httpx.get(f"{base}/api/libraries/{lib['id']}/search", params={"q": q, "limit": 12},
                          headers=h, timeout=10)
            for hit in (r.json().get("book") or []):
                item = hit.get("libraryItem") or {}
                md = (item.get("media") or {}).get("metadata") or {}
                found.append({"title": md.get("title"), "author": md.get("authorName"), "kind": "audiobook",
                              "tier": 1, "status": "owned", "source": "Audiobookshelf",
                              "action": {"type": "link", "url": f"{base}/item/{item.get('id')}"}})
        return found
    return cached(("abs", q.lower()), 120, fetch)


def src_gutenberg(q, user):
    def fetch():
        r = httpx.get(f"{GUTENDEX}/books", params={"search": q}, timeout=20, headers=HEADERS,
                      follow_redirects=True)
        r.raise_for_status()
        out = []
        for b in r.json().get("results", [])[:15]:
            fmts = b.get("formats", {})
            if not any(t.startswith("application/epub+zip") for t in fmts):
                continue
            out.append({"title": b.get("title"),
                        "author": " & ".join(flip_name(a["name"]) for a in b.get("authors", [])) or None,
                        "kind": "ebook", "tier": 2, "status": "free", "source": "Project Gutenberg",
                        "cover": fmts.get("image/jpeg"),
                        "action": {"type": "gutenberg", "id": b["id"]}})
        return out
    return cached(("gutenberg", q.lower()), 900, fetch)


def src_free_audio(q, user):
    def fetch():
        res = audio.search(q)["results"][:12]
        return [{"title": r["title"], "author": r["author"], "kind": "audiobook", "tier": 2, "status": "free",
                 "source": "LibriVox" if r["librivox"] else "Internet Archive", "cover": r["cover"],
                 "action": {"type": "audio", "id": r["id"], "done": bool(r["in_library"])}} for r in res]
    return cached(("audio", q.lower()), 900, fetch)


def _wait_days(item):
    for k in ("estimatedWaitDays", "estimatedWaitTime"):
        v = item.get(k)
        if isinstance(v, (int, float)) and v >= 0:
            return int(v)
    holds, copies = item.get("holdsCount") or 0, item.get("ownedCopies") or 0
    if copies:
        return int(math.ceil((holds + 1) / copies) * 14)  # Libby loans are usually 14 days
    return None


def src_libby(q, user, lib):
    """Opt-in only. Reads the public catalog for one library (no login, no card number)."""
    key = lib["libby_key"]

    def fetch():
        with _libby_gate:
            r = httpx.get(f"{THUNDER}/libraries/{key}/media", params={"query": q, "perPage": 20},
                          headers=HEADERS, timeout=15)
        r.raise_for_status()
        out = []
        for it in r.json().get("items", []):
            t = ((it.get("type") or {}).get("id") or "").lower()
            if t not in ("ebook", "audiobook"):
                continue
            available = bool(it.get("isAvailable")) or (it.get("availableCopies") or 0) > 0
            formats = {(f.get("id") or "") for f in (it.get("formats") or [])}
            out.append({
                "title": it.get("title"), "author": it.get("firstCreatorName"), "kind": t,
                "tier": KIND_RANK.get(lib["kind"], 5), "status": "available" if available else "wait",
                "wait_days": None if available else _wait_days(it),
                "holds": it.get("holdsCount"), "copies": it.get("ownedCopies"),
                "source": lib["name"], "library_kind": lib["kind"], "fee": lib.get("fee"),
                "kindle": (t != "ebook") or not formats or "ebook-kindle" in formats,
                "cover": ((it.get("covers") or {}).get("cover150Wide") or {}).get("href"),
                "action": {"type": "link", "url": libby_search_url(key, it.get("title") or q)},
            })
        return out
    return cached(("libby", key, q.lower()), 1800, fetch)


# ---------------------------------------------------------------- the search

def find(q, user):
    q = (q or "").strip()[:120]
    p = prefs()
    libs = libraries()
    jobs = {
        "Our library": (src_local, ()), "Purchases": (src_owned_imports, ()),
        "Audiobookshelf": (src_audiobookshelf, ()), "Project Gutenberg": (src_gutenberg, ()),
        "LibriVox / Internet Archive": (src_free_audio, ()),
    }
    if p["libby_lookup"]:
        for lib in [lib for lib in libs if lib.get("libby_key")][:15]:
            jobs[f"Libby: {lib['name']}"] = (src_libby, (lib,))

    options, status = [], {}
    futures = {_pool.submit(fn, q, user, *extra): name for name, (fn, extra) in jobs.items()}
    try:
        for fut in as_completed(futures, timeout=30):
            name = futures[fut]
            try:
                options += fut.result()
                status[name] = "ok"
            except Exception as e:  # noqa: BLE001 - one broken source shouldn't break search
                status[name] = f"unavailable ({type(e).__name__})"
    except TimeoutError:
        for fut, name in futures.items():
            status.setdefault(name, "too slow, skipped")
    return {"query": q, "groups": group(q, options, p, libs), "sources": status,
            "libby_lookup": p["libby_lookup"],
            "check_links": [{"name": lib["name"], "kind": lib["kind"], "fee": lib.get("fee"),
                             "url": libby_search_url(lib["libby_key"], q)} for lib in libs if lib.get("libby_key")],
            "buy": buy_links(q)}


def group(q, options, p, libs):
    groups = {}
    for o in options:
        if not o.get("title"):
            continue
        key = norm_title(o["title"])
        ak = author_key(o.get("author"))
        # Merge with an existing group of the same title whose author matches (or is unknown).
        match = next((k for k in groups if k[0] == key and (not ak or not k[1] or k[1] == ak)), None)
        k = match or (key, ak)
        g = groups.setdefault(k, {"title": o["title"], "author": o.get("author"), "options": [], "cover": None,
                                  "_rank": (9, 999)})
        # Name the group after the best-ranked option, preferring the shortest (cleanest) title.
        rank = (o.get("tier", 9), len(re.sub(r"\s*\((unabridged|abridged)\)", "", o["title"], flags=re.I)))
        if rank < g["_rank"]:
            g["title"] = re.sub(r"\s*\((unabridged|abridged)\)\s*$", "", o["title"], flags=re.I)
            g["_rank"] = rank
        if not g["author"] and o.get("author"):
            g["author"] = o["author"]
        if not g["cover"] and o.get("cover"):
            g["cover"] = o["cover"]
        if o.get("tier") == 1 and o["source"] == "Our library":
            g["title"], g["author"] = o["title"], o.get("author") or g["author"]
            g["_rank"] = (0, 0)
        g["options"].append(o)

    out = []
    for (key, ak), g in groups.items():
        rel = relevance(q, g["title"], g["author"] or "")
        if rel < 0.5 and not any(o["tier"] == 1 for o in g["options"]):
            continue  # loose matches from big catalogs
        g["relevance"] = rel
        g["kinds"] = {kind: plan(g["options"], kind, p, g["title"], g["author"])
                      for kind in ("ebook", "audiobook") if any(o["kind"] == kind for o in g["options"])}
        g["best_tier"] = min(v["best"]["tier"] if v["best"] else 9 for v in g["kinds"].values())
        g["check_links"] = [{"name": lib["name"], "kind": lib["kind"], "fee": lib.get("fee"),
                             "url": libby_search_url(lib["libby_key"], g["title"])}
                            for lib in libs if lib.get("libby_key")]
        del g["options"], g["_rank"]
        out.append(g)
    out.sort(key=lambda g: (g["best_tier"], -g["relevance"], g["title"]))
    return out[:15]


def plan(options, kind, p, title, author):
    """For one format: the best option, the queues worth showing, and whether to suggest buying."""
    opts = [o for o in options if o["kind"] == kind]
    ready = sorted([o for o in opts if o["status"] in ("owned", "free", "available")], key=lambda o: o["tier"])
    best = ready[0] if ready else None

    queues = []
    if not best:
        waits = [o for o in opts if o["status"] == "wait"]

        def shortest(tier):
            c = [o for o in waits if o["tier"] == tier]
            c = [o for o in c if o.get("kindle", True)] or c  # prefer copies that work on a Kindle
            return min(c, key=lambda o: (o.get("wait_days") is None, o.get("wait_days") or 0)) if c else None

        def days(o):
            return o["wait_days"] if o and o.get("wait_days") is not None else math.inf

        card, free, paid = shortest(3), shortest(4), shortest(5)
        if card:
            queues.append(card)
        if free and days(free) < days(card):
            queues.append(free)
        if paid and days(paid) < min(days(card), days(free)):
            queues.append(paid)

    best_wait = min((o.get("wait_days") for o in queues if o.get("wait_days") is not None), default=None)
    suggest_buy = not best and (best_wait is None or best_wait > p["wait_ok_days"])
    others = [o for o in ready[1:4]]
    q = f"{title} {author or ''}".strip()
    return {"best": best, "also": others, "queues": queues, "suggest_buy": suggest_buy,
            "buy": buy_links(q, kind) if suggest_buy else []}


# ---------------------------------------------------------------- imports of purchases

TITLE_COLS = ("title", "product name", "productname", "asin title", "book title", "name")
AUTHOR_COLS = ("author", "authors", "author name", "creator", "contributors")


def _pick(cols, wanted):
    low = {c.lower().strip(): c for c in cols if c}
    for w in wanted:
        if w in low:
            return low[w]
    for w in wanted:
        for lc, c in low.items():
            if w in lc:
                return c
    return None


def _rows_from_table(rows):
    rows = [r for r in rows if isinstance(r, dict)]
    if not rows:
        return []
    cols = set().union(*[r.keys() for r in rows[:50]])
    tc, ac = _pick(cols, TITLE_COLS), _pick(cols, AUTHOR_COLS)
    if not tc:
        return []
    return [(str(r.get(tc) or "").strip(), str(r.get(ac) or "").strip() if ac else "") for r in rows]


def _rows_from_bytes(name, data):
    name = name.lower()
    if name.endswith(".zip"):
        best = []
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for info in z.infolist():
                if info.file_size > 50_000_000 or not info.filename.lower().endswith((".csv", ".json", ".txt")):
                    continue
                got = _rows_from_bytes(info.filename, z.read(info))
                if len(got) > len(best):
                    best = got
        return best
    text = data.decode("utf-8-sig", errors="replace")
    if name.endswith(".json"):
        obj = json.loads(text)
        if isinstance(obj, dict):
            obj = next((v for v in obj.values() if isinstance(v, list)), [obj])
        return _rows_from_table(obj if isinstance(obj, list) else [])
    if name.endswith(".csv"):
        return _rows_from_table(list(csv.DictReader(io.StringIO(text))))
    out = []  # plain text: one book per line, "Title by Author" or "Title - Author"
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r"(.+?)\s+(?:by|-|—|\t)\s+(.+)$", line)
        out.append((m.group(1), m.group(2)) if m else (line, ""))
    return out


def import_owned(name, data, source, kind):
    rows = _rows_from_bytes(name, data)
    seen = {(r["norm"], r["kind"]) for r in db.q("SELECT norm, kind FROM owned_items")}
    added = 0
    for title, author in rows:
        if not title or len(title) > 300 or title.lower() in ("title", "n/a", "not available"):
            continue
        key = norm_title(title) + "|" + author_key(author)
        if (key, kind) in seen:
            continue
        seen.add((key, kind))
        db.x("INSERT INTO owned_items (source, kind, title, author, norm, created) VALUES (?,?,?,?,?,?)",
             (source, kind, title, author or None, key, time.time()))
        added += 1
    return {"found": len(rows), "added": added}


# Starting points for the "add a library" form. Fees change: always check the library's own page.
# Checked November 2026 against third-party guides; Libby links must be pasted by the user.
SUGGESTIONS = [
    {"name": "Fairfax County Public Library (VA)", "kind": "paid", "fee": "$50/year",
     "note": "Open to anyone; includes Libby.", "url": "https://outofstatelibrarycards.com/states/virginia"},
    {"name": "Monroe County Library System (Rochester, NY)", "kind": "paid", "fee": "about $25/year",
     "note": "US and Canada residents; Libby only (no Hoopla/Kanopy).",
     "url": "https://reciprocard.com/unlock-libby/non-resident-cards"},
    {"name": "Queens Public Library (NY)", "kind": "paid", "fee": "$50/year",
     "note": "Digital account; free for New York State residents.",
     "url": "https://reciprocard.com/unlock-libby/non-resident-cards"},
    {"name": "Virginia Beach Public Library (VA)", "kind": "paid", "fee": "$60/year or $15/3 months",
     "note": "Fee unlocks Libby for out-of-city cardholders.",
     "url": "https://outofstatelibrarycards.com/libraries/virginia-beach-public-library"},
    {"name": "Brooklyn Public Library (NY)", "kind": "free", "fee": "Free for ages 13-21",
     "note": "US residents ages 13-21 outside New York; otherwise $50/year.",
     "url": "https://reciprocard.com/unlock-libby/non-resident-cards"},
]
