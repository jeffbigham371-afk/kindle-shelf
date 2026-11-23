"""Kindle Shelf: fetch ebooks, convert them for Kindle, and deliver them.

Sources: Project Gutenberg search, any direct download URL, or a file upload.
Output: EPUB (for Amazon's Send to Kindle) and AZW3 (for USB / Kindle browser).
"""

import html
import json
import mimetypes
import os
import re
import secrets
import shutil
import smtplib
import sqlite3
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx
from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel

# ---------------------------------------------------------------- config

def env(name, default=None):
    v = os.environ.get(name)
    return v if v not in (None, "") else default

DATA_DIR = Path(env("DATA_DIR", "./data")).resolve()
INCOMING = DATA_DIR / "incoming"
LIBRARY = DATA_DIR / "library"
DB_PATH = DATA_DIR / "kindle-shelf.db"

APP_USER = env("APP_USER", "admin")
APP_PASSWORD = env("APP_PASSWORD")
MAX_DOWNLOAD_MB = int(env("MAX_DOWNLOAD_MB", "200"))
CONVERT_TIMEOUT = int(env("CONVERT_TIMEOUT", "900"))
OUTPUT_PROFILE = env("KINDLE_PROFILE", "kindle_pw3")
OUTPUT_FORMATS = [f.strip().lower() for f in env("OUTPUT_FORMATS", "epub,azw3").split(",") if f.strip()]
WORKERS = int(env("WORKERS", "2"))

KINDLE_EMAIL = env("KINDLE_EMAIL")
SMTP_HOST = env("SMTP_HOST")
SMTP_PORT = int(env("SMTP_PORT", "587"))
SMTP_USER = env("SMTP_USER")
SMTP_PASSWORD = env("SMTP_PASSWORD")
SMTP_FROM = env("SMTP_FROM", SMTP_USER)
SMTP_SECURITY = env("SMTP_SECURITY", "starttls").lower()  # starttls | ssl | none
AUTO_SEND = env("AUTO_SEND", "false").lower() in ("1", "true", "yes")

GUTENDEX = env("GUTENDEX_URL", "https://gutendex.com")
USER_AGENT = "KindleShelf/1.0 (self-hosted ebook converter)"

INPUT_EXTS = {
    ".epub", ".mobi", ".azw", ".azw3", ".prc", ".pdf", ".fb2", ".txt", ".html",
    ".htm", ".docx", ".rtf", ".odt", ".lit", ".cbz", ".cbr", ".kepub", ".pdb",
}
CONTENT_TYPE_EXT = {
    "application/epub+zip": ".epub",
    "application/x-mobipocket-ebook": ".mobi",
    "application/vnd.amazon.ebook": ".azw3",
    "application/pdf": ".pdf",
    "application/x-fictionbook+xml": ".fb2",
    "text/plain": ".txt",
    "text/html": ".html",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/rtf": ".rtf",
    "application/vnd.comicbook+zip": ".cbz",
}
# Amazon Send to Kindle accepts these; EPUB is preferred.
SEND_PREFERENCE = ["epub", "pdf", "docx", "txt"]
SEND_LIMIT_BYTES = 50 * 1024 * 1024

for d in (INCOMING, LIBRARY):
    d.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------- storage

_db_lock = threading.Lock()
_db = sqlite3.connect(DB_PATH, check_same_thread=False)
_db.row_factory = sqlite3.Row
_db.execute(
    """CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY,
        created REAL,
        status TEXT,          -- queued | downloading | converting | done | error
        message TEXT,
        source TEXT,
        title TEXT,
        author TEXT,
        files TEXT DEFAULT '{}',
        sent_at REAL,
        auto_send INTEGER DEFAULT 0
    )"""
)
_db.commit()
# Jobs that were mid-flight when the server stopped will never finish.
_db.execute("UPDATE jobs SET status='error', message='Interrupted by server restart' "
            "WHERE status IN ('queued','downloading','converting')")
_db.commit()


def db_update(job_id, **fields):
    if "files" in fields:
        fields["files"] = json.dumps(fields["files"])
    cols = ", ".join(f"{k}=?" for k in fields)
    with _db_lock:
        _db.execute(f"UPDATE jobs SET {cols} WHERE id=?", (*fields.values(), job_id))
        _db.commit()


def db_get(job_id):
    with _db_lock:
        row = _db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return row_to_dict(row) if row else None


def db_list():
    with _db_lock:
        rows = _db.execute("SELECT * FROM jobs ORDER BY created DESC").fetchall()
    return [row_to_dict(r) for r in rows]


def row_to_dict(row):
    d = dict(row)
    d["files"] = json.loads(d.get("files") or "{}")
    return d


def new_job(source, title=None, author=None, auto_send=False):
    job_id = uuid.uuid4().hex[:12]
    with _db_lock:
        _db.execute(
            "INSERT INTO jobs (id, created, status, message, source, title, author, auto_send) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (job_id, time.time(), "queued", "Waiting in queue", source, title, author, int(auto_send)),
        )
        _db.commit()
    return job_id

# ---------------------------------------------------------------- pipeline

def safe_name(s, fallback="book"):
    s = re.sub(r"[^\w\s.,'()-]", "", s or "", flags=re.UNICODE).strip()
    s = re.sub(r"\s+", " ", s)[:120]
    return s or fallback


def guess_filename(resp, url):
    cd = resp.headers.get("content-disposition", "")
    m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)\"?", cd, re.I)
    name = unquote(m.group(1)) if m else unquote(Path(urlparse(str(resp.url)).path).name)
    stem, ext = os.path.splitext(name)
    if ext.lower() not in INPUT_EXTS:
        ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
        ext = CONTENT_TYPE_EXT.get(ctype) or mimetypes.guess_extension(ctype) or ""
    # Gutenberg serves e.g. "pg1342-images-3.epub" or "1342.epub3.images"
    if ext.lower() not in INPUT_EXTS and ".epub" in url.lower():
        ext = ".epub"
    return safe_name(stem or "download") + ext.lower()


def download(job_id, url):
    db_update(job_id, status="downloading", message="Downloading…")
    dest_dir = INCOMING / job_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    limit = MAX_DOWNLOAD_MB * 1024 * 1024
    with httpx.stream("GET", url, follow_redirects=True, timeout=60,
                      headers={"User-Agent": USER_AGENT}) as resp:
        resp.raise_for_status()
        name = guess_filename(resp, url)
        if Path(name).suffix not in INPUT_EXTS:
            raise ValueError(
                f"That link doesn't look like an ebook (got {resp.headers.get('content-type', 'unknown type')}). "
                "Use a direct link to an EPUB/MOBI/PDF/etc. file."
            )
        path = dest_dir / name
        total = 0
        with open(path, "wb") as f:
            for chunk in resp.iter_bytes(65536):
                total += len(chunk)
                if total > limit:
                    raise ValueError(f"File is larger than MAX_DOWNLOAD_MB ({MAX_DOWNLOAD_MB} MB)")
                f.write(chunk)
    return path


def read_metadata(path):
    try:
        out = subprocess.run(["ebook-meta", str(path)], capture_output=True, text=True, timeout=120).stdout
    except Exception:
        return None, None
    meta = {}
    for line in out.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip().lower()] = v.strip()
    author = meta.get("author(s)")
    if author:
        author = re.sub(r"\s*\[.*?\]", "", author)  # drop "[Sort, Name]"
    return meta.get("title"), author


def convert(job_id, src):
    db_update(job_id, status="converting", message="Converting for Kindle…")
    job = db_get(job_id)
    title, author = read_metadata(src)
    title = job["title"] or title or src.stem
    author = job["author"] or author
    db_update(job_id, title=title, author=author)

    out_dir = LIBRARY / job_id
    out_dir.mkdir(parents=True, exist_ok=True)
    base = safe_name(f"{title} - {author}" if author else title)
    files = {}
    errors = []
    for fmt in OUTPUT_FORMATS:
        out = out_dir / f"{base}.{fmt}"
        db_update(job_id, message=f"Converting to {fmt.upper()}…")
        cmd = ["ebook-convert", str(src), str(out), "--output-profile", OUTPUT_PROFILE]
        if fmt == "epub":
            # Smaller, cleaner EPUBs that Send to Kindle accepts reliably.
            cmd += ["--no-default-epub-cover", "--epub-flatten"] if src.suffix == ".epub" else []
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=CONVERT_TIMEOUT,
                              env={**os.environ, "QT_QPA_PLATFORM": "offscreen"})
        if proc.returncode == 0 and out.exists():
            files[fmt] = out.name
        else:
            tail = (proc.stderr or proc.stdout).strip().splitlines()[-3:]
            errors.append(f"{fmt}: " + " ".join(tail))
    if not files:
        raise RuntimeError("Conversion failed. " + " | ".join(errors))
    original = out_dir / f"original{src.suffix}"
    shutil.copy2(src, original)
    files["original"] = original.name
    shutil.rmtree(INCOMING / job_id, ignore_errors=True)
    return files, errors


def smtp_configured():
    return bool(KINDLE_EMAIL and SMTP_HOST and SMTP_FROM)


def send_to_kindle(job, to=None):
    if not smtp_configured():
        raise RuntimeError("Email delivery is not configured (set KINDLE_EMAIL and SMTP_* settings)")
    fmt = next((f for f in SEND_PREFERENCE if f in job["files"]), None)
    if not fmt:
        raise RuntimeError("No Send-to-Kindle compatible file (EPUB/PDF/DOCX/TXT) for this book")
    path = LIBRARY / job["id"] / job["files"][fmt]
    if path.stat().st_size > SEND_LIMIT_BYTES:
        raise RuntimeError("File is over Amazon's 50 MB email limit; download it and use USB instead")

    msg = EmailMessage()
    msg["From"] = SMTP_FROM
    msg["To"] = to or KINDLE_EMAIL
    msg["Subject"] = job["title"] or "Book"
    msg.set_content(f"{job['title']}\n\nSent by Kindle Shelf.")
    ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    if fmt == "epub":
        ctype = "application/epub+zip"
    maintype, subtype = ctype.split("/", 1)
    msg.add_attachment(path.read_bytes(), maintype=maintype, subtype=subtype, filename=path.name)

    if SMTP_SECURITY == "ssl":
        server = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=60)
    else:
        server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=60)
        if SMTP_SECURITY == "starttls":
            server.starttls()
    with server:
        if SMTP_USER:
            server.login(SMTP_USER, SMTP_PASSWORD or "")
        server.send_message(msg)
    db_update(job["id"], sent_at=time.time())


def run_job(job_id, url=None, local_path=None):
    try:
        src = download(job_id, url) if url else Path(local_path)
        files, errors = convert(job_id, src)
        msg = "Ready" + (f" (some formats failed: {'; '.join(errors)})" if errors else "")
        db_update(job_id, status="done", message=msg, files=files)
        job = db_get(job_id)
        if job["auto_send"] and smtp_configured():
            db_update(job_id, message="Emailing to Kindle…")
            send_to_kindle(job)
            db_update(job_id, message=f"Sent to {KINDLE_EMAIL}")
    except Exception as e:  # noqa: BLE001 - surface any failure to the UI
        db_update(job_id, status="error", message=str(e)[:500])
        shutil.rmtree(INCOMING / job_id, ignore_errors=True)


executor = ThreadPoolExecutor(max_workers=WORKERS)

# ---------------------------------------------------------------- web app

security = HTTPBasic(auto_error=False)


def require_auth(creds: HTTPBasicCredentials | None = Depends(security)):
    if not APP_PASSWORD:
        return
    ok = creds is not None and secrets.compare_digest(creds.username, APP_USER) \
        and secrets.compare_digest(creds.password, APP_PASSWORD)
    if not ok:
        raise HTTPException(401, "Authentication required", headers={"WWW-Authenticate": "Basic"})


app = FastAPI(title="Kindle Shelf", dependencies=[Depends(require_auth)])
STATIC = Path(__file__).parent / "static"


class JobRequest(BaseModel):
    url: str | None = None
    gutenberg_id: int | None = None
    send: bool | None = None


class SendRequest(BaseModel):
    to: str | None = None


@app.get("/", response_class=HTMLResponse)
def index():
    return (STATIC / "index.html").read_text()


@app.get("/api/config")
def config():
    return {
        "email_configured": smtp_configured(),
        "kindle_email": KINDLE_EMAIL,
        "auto_send": AUTO_SEND,
        "formats": OUTPUT_FORMATS,
        "profile": OUTPUT_PROFILE,
        "calibre": shutil.which("ebook-convert") is not None,
    }


def gutendex_book(b):
    fmts = b.get("formats", {})
    epub = next((u for t, u in fmts.items() if t.startswith("application/epub+zip")), None)
    return {
        "id": b["id"],
        "title": b.get("title"),
        "authors": ", ".join(a["name"] for a in b.get("authors", [])),
        "languages": b.get("languages", []),
        "downloads": b.get("download_count"),
        "cover": fmts.get("image/jpeg"),
        "epub": epub,
    }


@app.get("/api/search")
def search(q: str, page: int = 1):
    try:
        r = httpx.get(f"{GUTENDEX}/books", params={"search": q, "page": page},
                      timeout=30, headers={"User-Agent": USER_AGENT}, follow_redirects=True)
        r.raise_for_status()
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Project Gutenberg search failed: {e}")
    data = r.json()
    return {"count": data.get("count", 0), "next": bool(data.get("next")),
            "results": [gutendex_book(b) for b in data.get("results", [])]}


@app.post("/api/jobs")
def create_job(req: JobRequest):
    send = AUTO_SEND if req.send is None else req.send
    if req.gutenberg_id:
        r = httpx.get(f"{GUTENDEX}/books/{req.gutenberg_id}", timeout=30,
                      headers={"User-Agent": USER_AGENT}, follow_redirects=True)
        if r.status_code != 200:
            raise HTTPException(404, "Gutenberg book not found")
        book = gutendex_book(r.json())
        if not book["epub"]:
            raise HTTPException(400, "No EPUB available for that book")
        job_id = new_job(f"Gutenberg #{book['id']}", book["title"], book["authors"] or None, send)
        executor.submit(run_job, job_id, url=book["epub"])
    elif req.url:
        if urlparse(req.url).scheme not in ("http", "https"):
            raise HTTPException(400, "URL must start with http:// or https://")
        job_id = new_job(req.url, auto_send=send)
        executor.submit(run_job, job_id, url=req.url)
    else:
        raise HTTPException(400, "Provide a url or gutenberg_id")
    return db_get(job_id)


@app.post("/api/upload")
async def upload(file: UploadFile = File(...), send: bool = Form(False)):
    ext = Path(file.filename or "").suffix.lower()
    if ext not in INPUT_EXTS:
        raise HTTPException(400, f"Unsupported file type '{ext}'")
    job_id = new_job(f"Upload: {file.filename}", auto_send=send)
    dest_dir = INCOMING / job_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / (safe_name(Path(file.filename).stem) + ext)
    with open(dest, "wb") as f:
        shutil.copyfileobj(file.file, f)
    executor.submit(run_job, job_id, local_path=str(dest))
    return db_get(job_id)


@app.get("/api/jobs")
def list_jobs():
    return db_list()


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str):
    job = db_get(job_id)
    if not job:
        raise HTTPException(404)
    if job["status"] in ("queued", "downloading", "converting"):
        raise HTTPException(409, "Job is still running")
    shutil.rmtree(LIBRARY / job_id, ignore_errors=True)
    with _db_lock:
        _db.execute("DELETE FROM jobs WHERE id=?", (job_id,))
        _db.commit()
    return {"ok": True}


@app.post("/api/jobs/{job_id}/send")
def send_job(job_id: str, req: SendRequest | None = None):
    job = db_get(job_id)
    if not job or job["status"] != "done":
        raise HTTPException(404, "Book not ready")
    try:
        send_to_kindle(job, (req.to if req else None))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, str(e))
    return db_get(job_id)


@app.get("/api/jobs/{job_id}/file/{fmt}")
@app.get("/books/{job_id}/{fmt}")
def get_file(job_id: str, fmt: str):
    job = db_get(job_id)
    if not job or fmt not in job["files"]:
        raise HTTPException(404)
    path = LIBRARY / job_id / job["files"][fmt]
    media = "application/epub+zip" if fmt == "epub" else (
        "application/vnd.amazon.ebook" if fmt == "azw3" else None)
    return FileResponse(path, filename=path.name, media_type=media)


@app.get("/kindle", response_class=HTMLResponse)
def kindle_page():
    """Bare-bones page for the Kindle's experimental web browser.

    Tapping an AZW3 link there downloads the book straight onto the device.
    """
    rows = []
    for j in db_list():
        if j["status"] != "done" or "azw3" not in j["files"]:
            continue
        t = html.escape(j["title"] or "Untitled")
        a = html.escape(j["author"] or "")
        rows.append(f'<li><a href="/books/{j["id"]}/azw3">{t}</a><br><small>{a}</small></li>')
    body = "\n".join(rows) or "<li>No books yet.</li>"
    return (
        "<!doctype html><html><head><meta charset='utf-8'><title>Kindle Shelf</title>"
        "<meta name='viewport' content='width=device-width'>"
        "<style>body{font-family:serif;font-size:1.2em;margin:1em}li{margin:0 0 1em}</style>"
        f"</head><body><h1>Kindle Shelf</h1><p>Tap a title to download it.</p><ul>{body}</ul></body></html>"
    )
