"""Audiobooks: find free ones (LibriVox / Internet Archive) or take uploads, and file them
into an Audiobookshelf library folder as <Author>/<Title>/ with cover.jpg and desc.txt.

Audiobookshelf watches that folder and adds new books by itself; optionally we also ask it
to rescan through its API.
"""

import re
import shutil
import time
import uuid
import zipfile
from pathlib import Path
from urllib.parse import quote

import httpx

from . import db
from .config import DATA_DIR, INCOMING, USER_AGENT, env
from .library import executor, flip_name, html_to_text, safe_name

IA = env("ARCHIVE_URL", "https://archive.org")
MAX_AUDIOBOOK_MB = int(env("MAX_AUDIOBOOK_MB", "4000"))
AUDIO_EXTS = {".mp3", ".m4b", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma", ".wav"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
DEFAULT_DIR = env("AUDIOBOOK_DIR", str(DATA_DIR / "audiobooks"))
HEADERS = {"User-Agent": USER_AGENT}


def migrate():
    db.x("""CREATE TABLE IF NOT EXISTS audiobooks (
        id TEXT PRIMARY KEY, created REAL, status TEXT, message TEXT, source TEXT,
        title TEXT, author TEXT, narrator TEXT, description TEXT, path TEXT,
        file_count INTEGER, size INTEGER, added_by INTEGER)""")
    db.x("UPDATE audiobooks SET status='error', message='Interrupted by server restart - try again' "
         "WHERE status IN ('queued','downloading')")


def settings():
    return {
        "audiobook_dir": db.get_setting("audiobook_dir", DEFAULT_DIR),
        "abs_url": db.get_setting("abs_url", env("ABS_URL")),
        "abs_token": db.get_setting("abs_token", env("ABS_TOKEN")),
        "audio_quality": db.get_setting("audio_quality", "small"),  # small = 64k MP3, high = VBR MP3
    }


def target_dir():
    d = Path(settings()["audiobook_dir"])
    d.mkdir(parents=True, exist_ok=True)
    return d


def check_dir(path):
    """Make sure the folder exists and we can write to it."""
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    probe = p / f".kindle-shelf-test-{uuid.uuid4().hex[:6]}"
    probe.write_text("ok")
    probe.unlink()
    return str(p.resolve())


# ---------------------------------------------------------------- search

def _creator(c):
    if isinstance(c, list):
        c = c[0] if c else None
    return flip_name(c) if c else None


def search(q, page=1):
    words = re.sub(r"[^\w\s'-]", " ", q, flags=re.UNICODE).strip()
    if not words:
        return {"count": 0, "results": []}
    query = (f"(title:({words}) OR creator:({words})) AND mediatype:audio AND "
             "(collection:librivoxaudio OR collection:audio_bookspoetry)")
    r = httpx.get(f"{IA}/advancedsearch.php", headers=HEADERS, timeout=30, follow_redirects=True, params={
        "q": query, "fl[]": ["identifier", "title", "creator", "downloads", "collection"],
        "sort[]": "downloads desc", "rows": 25, "page": page, "output": "json"})
    r.raise_for_status()
    resp = r.json().get("response", {})
    have = {a["source"]: a["id"] for a in db.q("SELECT id, source FROM audiobooks WHERE status != 'error'")}
    results = []
    for d in resp.get("docs", []):
        coll = d.get("collection") or []
        coll = coll if isinstance(coll, list) else [coll]
        results.append({
            "id": d["identifier"], "title": d.get("title"), "author": _creator(d.get("creator")),
            "downloads": d.get("downloads"), "librivox": "librivoxaudio" in coll,
            "cover": f"{IA}/services/img/{d['identifier']}",
            "page": f"{IA}/details/{d['identifier']}",
            "in_library": have.get(f"archive:{d['identifier']}"),
        })
    return {"count": resp.get("numFound", 0), "results": results}


def pick_files(files, quality="small"):
    """Choose which audio files to download from an Internet Archive item."""
    audio = [f for f in files if Path(f.get("name", "")).suffix.lower() in AUDIO_EXTS]
    m4b = [f for f in audio if f["name"].lower().endswith(".m4b")]
    if m4b:  # single-file audiobooks with chapters: what Audiobookshelf likes best
        return sorted(m4b, key=lambda f: f["name"])
    small = [f for f in audio if f.get("format") == "64Kbps MP3" or f["name"].lower().endswith("_64kb.mp3")]
    high = [f for f in audio if f.get("format") == "VBR MP3"]
    chosen = (small or high) if quality == "small" else (high or small)
    if not chosen:
        chosen = [f for f in audio if f["name"].lower().endswith(".mp3")] or \
                 [f for f in audio if f["name"].lower().endswith((".ogg", ".opus"))] or audio
    # Internet Archive lists derived files at the top level only; skip anything in sub-folders of samples
    return sorted(chosen, key=lambda f: f["name"])


# ---------------------------------------------------------------- pipeline

def _update(aid, **f):
    cols = ", ".join(f"{k}=?" for k in f)
    db.x(f"UPDATE audiobooks SET {cols} WHERE id=?", (*f.values(), aid))


def get(aid):
    return db.q("SELECT * FROM audiobooks WHERE id=?", (aid,), one=True)


def list_all():
    return db.q("SELECT * FROM audiobooks ORDER BY created DESC")


def _new(source, user, **fields):
    aid = uuid.uuid4().hex[:12]
    db.x("INSERT INTO audiobooks (id, created, status, message, source, added_by, title, author, narrator) "
         "VALUES (?,?,?,?,?,?,?,?,?)",
         (aid, time.time(), "queued", "Waiting in line…", source, user["id"] if user else None,
          fields.get("title"), fields.get("author"), fields.get("narrator")))
    return aid


def _stream(url, dest, budget):
    """Download url to dest, returning bytes written; stops if total would exceed budget."""
    written = 0
    with httpx.stream("GET", url, headers=HEADERS, timeout=120, follow_redirects=True) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_bytes(1 << 16):
                written += len(chunk)
                if written > budget:
                    raise ValueError(f"Audiobook is bigger than {MAX_AUDIOBOOK_MB} MB")
                f.write(chunk)
    return written


def queue_archive(identifier, user):
    if not re.fullmatch(r"[\w.-]+", identifier):
        raise ValueError("Bad item id")
    existing = db.q("SELECT id FROM audiobooks WHERE source=? AND status != 'error'",
                    (f"archive:{identifier}",), one=True)
    if existing:
        return existing["id"], True
    aid = _new(f"archive:{identifier}", user)
    executor.submit(_run_archive, aid, identifier)
    return aid, False


def _run_archive(aid, identifier):
    stage = INCOMING / f"audio-{aid}"
    try:
        _update(aid, status="downloading", message="Looking up the recording…")
        r = httpx.get(f"{IA}/metadata/{identifier}", headers=HEADERS, timeout=60, follow_redirects=True)
        r.raise_for_status()
        item = r.json()
        meta = item.get("metadata") or {}
        title = meta.get("title")
        title = title[0] if isinstance(title, list) else title
        author = _creator(meta.get("creator"))
        desc = meta.get("description")
        desc = html_to_text("\n\n".join(desc) if isinstance(desc, list) else desc)
        files = pick_files(item.get("files") or [], settings()["audio_quality"])
        if not files:
            raise ValueError("No audio files found in this recording")
        _update(aid, title=title or identifier, author=author, description=desc)

        stage.mkdir(parents=True, exist_ok=True)
        budget, total = MAX_AUDIOBOOK_MB * 1024 * 1024, 0
        width = len(str(len(files)))
        for i, f in enumerate(files, 1):
            _update(aid, message=f"Downloading part {i} of {len(files)}…")
            name = Path(f["name"]).name
            # Number the files so the play order is always right.
            local = stage / f"{str(i).zfill(width)} - {safe_name(Path(name).stem, 'part', 100)}{Path(name).suffix.lower()}" \
                if len(files) > 1 else stage / f"{safe_name(title or 'audiobook', 'audiobook', 100)}{Path(name).suffix.lower()}"
            total += _stream(f"{IA}/download/{quote(identifier)}/{quote(f['name'])}", local, budget - total)
        try:
            c = httpx.get(f"{IA}/services/img/{identifier}", headers=HEADERS, timeout=30, follow_redirects=True)
            if c.status_code == 200 and c.headers.get("content-type", "").startswith("image/") and len(c.content) > 2000:
                (stage / "cover.jpg").write_bytes(c.content)
        except httpx.HTTPError:
            pass
        _finish(aid, stage, len(files), total)
    except Exception as e:  # noqa: BLE001
        _update(aid, status="error", message=str(e)[:400])
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def queue_upload(paths, title, author, narrator, user):
    aid = _new(f"upload:{user['name']}", user, title=title, author=author, narrator=narrator)
    executor.submit(_run_upload, aid, [Path(p) for p in paths])
    return aid


def _safe_extract(zip_path, dest):
    count = 0
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            name = Path(info.filename)
            if info.is_dir() or name.suffix.lower() not in AUDIO_EXTS | IMAGE_EXTS:
                continue
            if name.name.startswith("._"):  # macOS junk
                continue
            out = dest / safe_name(name.name, "file", 150)
            with z.open(info) as src, open(out, "wb") as f:
                shutil.copyfileobj(src, f)
            count += 1
    return count


def _run_upload(aid, paths):
    stage = INCOMING / f"audio-{aid}"
    try:
        _update(aid, status="downloading", message="Unpacking…")
        stage.mkdir(parents=True, exist_ok=True)
        for p in paths:
            if p.suffix.lower() == ".zip":
                _safe_extract(p, stage)
            elif p.suffix.lower() in AUDIO_EXTS | IMAGE_EXTS:
                shutil.move(str(p), str(stage / p.name))
        audio = [f for f in stage.iterdir() if f.suffix.lower() in AUDIO_EXTS]
        if not audio:
            raise ValueError("No audio files found (MP3, M4B, M4A, FLAC, OGG…)")
        images = [f for f in stage.iterdir() if f.suffix.lower() in IMAGE_EXTS]
        if images and not (stage / "cover.jpg").exists():
            best = max(images, key=lambda f: f.stat().st_size)
            best.rename(stage / f"cover{best.suffix.lower()}")
        total = sum(f.stat().st_size for f in audio)
        _finish(aid, stage, len(audio), total)
    except Exception as e:  # noqa: BLE001
        _update(aid, status="error", message=str(e)[:400])
    finally:
        shutil.rmtree(stage, ignore_errors=True)
        for p in paths:
            shutil.rmtree(p.parent, ignore_errors=True)


def _finish(aid, stage, count, size):
    """Write desc/reader files and move the finished folder into the Audiobookshelf library."""
    a = get(aid)
    if a.get("description"):
        (stage / "desc.txt").write_text(a["description"], encoding="utf-8")
    if a.get("narrator"):
        (stage / "reader.txt").write_text(a["narrator"], encoding="utf-8")
    _update(aid, message="Moving into the audiobook library…")
    root = target_dir()
    author_dir = root / safe_name(a.get("author"), "Unknown Author")
    dest, n = author_dir / safe_name(a.get("title"), "Untitled Audiobook"), 2
    base = dest
    while dest.exists():
        dest = base.with_name(f"{base.name} ({n})")
        n += 1
    author_dir.mkdir(parents=True, exist_ok=True)
    shutil.move(str(stage), str(dest))
    _update(aid, status="done", path=str(dest), file_count=count, size=size,
            message="In your Audiobookshelf library")
    msg = trigger_scan()
    if msg:
        _update(aid, message=msg)


def trigger_scan():
    """Ask Audiobookshelf to rescan its book libraries (only if URL + API key are set)."""
    s = settings()
    if not (s["abs_url"] and s["abs_token"]):
        return None
    base = s["abs_url"].rstrip("/")
    h = {"Authorization": f"Bearer {s['abs_token']}", **HEADERS}
    try:
        libs = httpx.get(f"{base}/api/libraries", headers=h, timeout=20).json().get("libraries", [])
        n = 0
        for lib in libs:
            if lib.get("mediaType", "book") == "book":
                httpx.post(f"{base}/api/libraries/{lib['id']}/scan", headers=h, timeout=20)
                n += 1
        return "In your Audiobookshelf library (scan started)" if n else None
    except Exception:  # noqa: BLE001
        return "In your Audiobookshelf folder (couldn't reach Audiobookshelf to rescan; it will pick it up on its own)"
