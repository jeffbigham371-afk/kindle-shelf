"""The book pipeline: fetch -> read metadata -> convert -> file into the library -> email.

Library layout on the server's disk:
    <LIBRARY>/<Author>/<Title>/<Title> - <Author>.epub
                              /<Title> - <Author>.azw3
                              /original.<ext>
                              /cover.jpg
"""

import html
import mimetypes
import os
import re
import shutil
import smtplib
import subprocess
import tempfile
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx

from . import db
from .config import (
    CONVERT_TIMEOUT, IMPORT_DIR, IMPORT_SCAN_SECONDS, INCOMING, INPUT_EXTS, LIBRARY,
    MAX_DOWNLOAD_MB, OUTPUT_FORMATS, OUTPUT_PROFILE, SMTP_DEFAULTS, USER_AGENT, WORKERS,
)

META_VERSION = 1
SEND_PREFERENCE = ["epub", "pdf", "docx", "txt"]  # formats Amazon's Send to Kindle accepts
SEND_LIMIT_BYTES = 50 * 1024 * 1024
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
CALIBRE_ENV = {**os.environ, "QT_QPA_PLATFORM": "offscreen"}

executor = ThreadPoolExecutor(max_workers=WORKERS)
_fs_lock = threading.Lock()  # serialises folder creation / moves inside the library


# ---------------------------------------------------------------- helpers

def safe_name(s, fallback="Untitled", limit=80):
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", s or "")  # illegal on Windows
    s = re.sub(r"\s+", " ", s).strip().rstrip(". ")
    return s[:limit].rstrip(". ") or fallback


def flip_name(name):
    """Gutenberg lists authors as 'Austen, Jane' -> 'Jane Austen'."""
    if name and name.count(",") == 1:
        last, first = [p.strip() for p in name.split(",")]
        if first and last and not re.search(r"\d", first):
            return f"{first} {last}"
    return name


def html_to_text(s):
    if not s:
        return None
    s = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</h\d>", "\n\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n\n", s).strip()
    return s or None


def run_calibre(args, timeout=CONVERT_TIMEOUT):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, env=CALIBRE_ENV)


def book_dir(book):
    return LIBRARY / (book.get("path") or book["id"])


# ---------------------------------------------------------------- metadata

def read_metadata(src):
    """Use Calibre's ebook-meta to read title/author/description/series/cover from a file."""
    meta, cover = {}, None
    tmp = Path(tempfile.mkdtemp(prefix="ks-meta-"))
    try:
        opf, cov = tmp / "meta.opf", tmp / "cover.jpg"
        run_calibre(["ebook-meta", str(src), f"--to-opf={opf}", f"--get-cover={cov}"], timeout=180)
        if cov.exists() and cov.stat().st_size > 1000:
            cover = cov.read_bytes()
        if opf.exists():
            meta = parse_opf(opf)
    except Exception:  # noqa: BLE001 - metadata is best effort
        pass
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return meta, cover


def parse_opf(path):
    ns = {"dc": "http://purl.org/dc/elements/1.1/", "opf": "http://www.idpf.org/2007/opf"}
    md = ET.parse(path).getroot().find("opf:metadata", ns)
    if md is None:
        return {}
    out = {
        "title": (md.findtext("dc:title", namespaces=ns) or "").strip() or None,
        "author": " & ".join(e.text.strip() for e in md.findall("dc:creator", ns) if e.text) or None,
        "description": html_to_text(md.findtext("dc:description", namespaces=ns)),
        "language": md.findtext("dc:language", namespaces=ns),
        "tags": [e.text.strip() for e in md.findall("dc:subject", ns) if e.text and len(e.text) < 40][:8],
    }
    date = md.findtext("dc:date", namespaces=ns) or ""
    if re.match(r"\d{4}", date) and int(date[:4]) > 1000:
        out["published"] = date[:4]
    for m in md.findall("opf:meta", ns):
        if m.get("name") == "calibre:series":
            out["series"] = m.get("content")
        elif m.get("name") == "calibre:series_index":
            try:
                out["series_index"] = float(m.get("content"))
            except (TypeError, ValueError):
                pass
    if out["title"] and out["title"].lower() in ("unknown", "untitled"):
        out["title"] = None
    if out["author"] and out["author"].lower() == "unknown":
        out["author"] = None
    return out


def write_metadata(book):
    """Push title/author/series/description/cover from the database into the book files,
    so the Kindle shows the corrected details."""
    folder = book_dir(book)
    cover = folder / book["cover"] if book.get("cover") else None
    for fmt in ("epub", "azw3"):
        name = book["files"].get(fmt)
        if not name:
            continue
        args = ["ebook-meta", str(folder / name), "-t", book["title"] or "Untitled",
                "-a", book["author"] or "Unknown"]
        if book.get("series"):
            args += ["--series", book["series"]]
            if book.get("series_index") is not None:
                args += ["--index", str(book["series_index"])]
        if book.get("description"):
            args += ["-c", book["description"]]
        if cover and cover.exists():
            args += ["--cover", str(cover)]
        run_calibre(args, timeout=180)


# ---------------------------------------------------------------- library folders

def target_folder(title, author, book_id):
    """Pick <Author>/<Title> for a book, adding ' (2)' etc. if another book already has it."""
    base = Path(safe_name(author, "Unknown Author")) / safe_name(title)
    rel, n = base, 2
    while True:
        owner = db.q("SELECT id FROM books WHERE path=?", (str(rel.as_posix()),), one=True)
        if (owner is None or owner["id"] == book_id) and (owner or not (LIBRARY / rel).exists()):
            return rel.as_posix()
        rel = base.with_name(f"{base.name} ({n})")
        n += 1


def file_base(title, author):
    return safe_name(f"{title} - {author}" if author else title, limit=120)


def relocate(book_id):
    """Move a book's folder and rename its files to match its current title/author."""
    with _fs_lock:
        book = db.get_book(book_id)
        old = book_dir(book)
        new_rel = target_folder(book["title"], book["author"], book_id)
        new = LIBRARY / new_rel
        if old.resolve() != new.resolve() and old.exists():
            new.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(old), str(new))
            _remove_empty_parents(old.parent)
        files, base = dict(book["files"]), file_base(book["title"], book["author"])
        for fmt in OUTPUT_FORMATS:
            if fmt in files:
                want = f"{base}.{fmt}"
                if files[fmt] != want and (new / files[fmt]).exists():
                    (new / files[fmt]).rename(new / want)
                    files[fmt] = want
        db.update_book(book_id, path=new_rel, files=files)


def _remove_empty_parents(folder):
    try:
        while folder != LIBRARY and folder.is_dir() and not any(folder.iterdir()):
            folder.rmdir()
            folder = folder.parent
    except OSError:
        pass


def delete_files(book):
    with _fs_lock:
        folder = book_dir(book)
        if folder.exists() and folder.resolve() != LIBRARY.resolve():
            shutil.rmtree(folder, ignore_errors=True)
            _remove_empty_parents(folder.parent)


# ---------------------------------------------------------------- pipeline

def guess_filename(resp, url):
    cd = resp.headers.get("content-disposition", "")
    m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)\"?", cd, re.I)
    name = unquote(m.group(1)) if m else unquote(Path(urlparse(str(resp.url)).path).name)
    stem, ext = os.path.splitext(name)
    if ext.lower() not in INPUT_EXTS:
        ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
        ext = CONTENT_TYPE_EXT.get(ctype) or mimetypes.guess_extension(ctype) or ""
    if ext.lower() not in INPUT_EXTS and ".epub" in url.lower():
        ext = ".epub"  # Gutenberg links like 1342.epub3.images
    return safe_name(stem, "download") + ext.lower()


def download(book_id, url):
    db.update_book(book_id, status="downloading", message="Downloading…")
    dest = INCOMING / book_id
    dest.mkdir(parents=True, exist_ok=True)
    limit = MAX_DOWNLOAD_MB * 1024 * 1024
    with httpx.stream("GET", url, follow_redirects=True, timeout=60,
                      headers={"User-Agent": USER_AGENT}) as resp:
        resp.raise_for_status()
        name = guess_filename(resp, url)
        if Path(name).suffix not in INPUT_EXTS:
            raise ValueError("That link doesn't lead to an ebook file "
                             f"(it returned {resp.headers.get('content-type', 'an unknown type')}).")
        path, total = dest / name, 0
        with open(path, "wb") as f:
            for chunk in resp.iter_bytes(65536):
                total += len(chunk)
                if total > limit:
                    raise ValueError(f"The file is bigger than {MAX_DOWNLOAD_MB} MB")
                f.write(chunk)
    return path


def fetch_cover(url):
    try:
        r = httpx.get(url, timeout=30, follow_redirects=True, headers={"User-Agent": USER_AGENT})
        if r.status_code == 200 and r.headers.get("content-type", "").startswith("image/"):
            return r.content
    except httpx.HTTPError:
        pass
    return None


def ingest(book_id, src):
    """Read metadata, convert, and file the book into its library folder."""
    db.update_book(book_id, status="converting", message="Reading book details…")
    hint = db.get_book(book_id)
    meta, cover = read_metadata(src)
    if not cover and hint.get("cover_url"):
        cover = fetch_cover(hint["cover_url"])

    fields = {k: meta.get(k) for k in ("description", "series", "series_index", "language", "published")}
    fields["title"] = hint.get("title") or meta.get("title") or src.stem
    fields["author"] = hint.get("author") or meta.get("author")
    fields["description"] = hint.get("description") or fields["description"]
    fields["tags"] = meta.get("tags") or []
    fields["meta_version"] = META_VERSION
    db.update_book(book_id, **fields)

    with _fs_lock:
        rel = target_folder(fields["title"], fields["author"], book_id)
        folder = LIBRARY / rel
        folder.mkdir(parents=True, exist_ok=True)
        db.update_book(book_id, path=rel)

    files, errors = {}, []
    if cover:
        (folder / "cover.jpg").write_bytes(cover)
        db.update_book(book_id, cover="cover.jpg")
    base = file_base(fields["title"], fields["author"])
    for fmt in OUTPUT_FORMATS:
        db.update_book(book_id, message=f"Making the {fmt.upper()} version…")
        out = folder / f"{base}.{fmt}"
        cmd = ["ebook-convert", str(src), str(out), "--output-profile", OUTPUT_PROFILE,
               "--title", fields["title"]]
        if fields["author"]:
            cmd += ["--authors", fields["author"]]
        if cover:
            cmd += ["--cover", str(folder / "cover.jpg")]
        proc = run_calibre(cmd)
        if proc.returncode == 0 and out.exists():
            files[fmt] = out.name
        else:
            tail = (proc.stderr or proc.stdout).strip().splitlines()[-2:]
            errors.append(f"{fmt.upper()}: " + " ".join(tail))
    if not files:
        raise RuntimeError("Couldn't convert this book. " + " | ".join(errors))
    original = folder / f"original{src.suffix.lower()}"
    shutil.copy2(src, original)
    files["original"] = original.name
    return files, errors


def run_book(book_id, url=None, local_path=None):
    try:
        src = download(book_id, url) if url else Path(local_path)
        files, errors = ingest(book_id, src)
        msg = "Ready" + (f" (some versions failed: {'; '.join(errors)})" if errors else "")
        db.update_book(book_id, status="done", message=msg, files=files)
        book = db.get_book(book_id)
        if book.get("auto_send") and book.get("added_by"):
            user = db.q("SELECT * FROM users WHERE id=?", (book["added_by"],), one=True)
            if user and user.get("kindle_email") and email_ready():
                db.update_book(book_id, message="Sending to Kindle…")
                try:
                    send_to_kindle(book, user)
                    db.update_book(book_id, message=f"Sent to {user['name']}'s Kindle")
                except Exception as e:  # noqa: BLE001
                    db.update_book(book_id, message=f"Ready, but sending failed: {e}")
    except Exception as e:  # noqa: BLE001 - show any failure in the UI
        db.update_book(book_id, status="error", message=str(e)[:500])
    finally:
        shutil.rmtree(INCOMING / book_id, ignore_errors=True)
        if local_path and Path(local_path).parent.parent == INCOMING:
            shutil.rmtree(Path(local_path).parent, ignore_errors=True)


def queue_book(*, url=None, local_path=None, user=None, send=False, **hints):
    book_id = uuid.uuid4().hex[:12]
    db.insert_book(book_id, added_by=user["id"] if user else None, auto_send=int(bool(send)), **hints)
    executor.submit(run_book, book_id, url=url, local_path=local_path)
    return book_id


def refresh_old_books():
    """Books added by version 1 have no cover/description and live in <id>/ folders.
    Read their details and move them into the Author/Title layout."""
    for b in db.q(f"SELECT id FROM books WHERE status='done' AND COALESCE(meta_version,0) < {META_VERSION}"):
        executor.submit(_refresh_one, b["id"])


def _refresh_one(book_id):
    try:
        book = db.get_book(book_id)
        orig = book["files"].get("original")
        if not orig:
            return
        meta, cover = read_metadata(book_dir(book) / orig)
        upd = {k: meta.get(k) for k in ("description", "series", "series_index", "language", "published")}
        upd["tags"] = meta.get("tags") or []
        upd["meta_version"] = META_VERSION
        if cover:
            (book_dir(book) / "cover.jpg").write_bytes(cover)
            upd["cover"] = "cover.jpg"
        db.update_book(book_id, **upd)
        relocate(book_id)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------- import folder

def scan_import_folder(user=None):
    """Queue every ebook found in IMPORT_DIR (files are moved out, so each is added once)."""
    added = 0
    for path in sorted(IMPORT_DIR.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in INPUT_EXTS:
            continue
        try:
            size = path.stat().st_size
            time.sleep(0.5)
            if path.stat().st_size != size or time.time() - path.stat().st_mtime < 5:
                continue  # still being copied in
            book_id = uuid.uuid4().hex[:12]
            dest = INCOMING / book_id
            dest.mkdir(parents=True, exist_ok=True)
            moved = dest / (safe_name(path.stem, "book") + path.suffix.lower())
            shutil.move(str(path), str(moved))
        except OSError:
            continue  # locked by Windows / another program; try next scan
        db.insert_book(book_id, source=f"Import folder: {path.name}",
                       added_by=user["id"] if user else None, auto_send=0)
        executor.submit(run_book, book_id, local_path=str(moved))
        added += 1
    for d in sorted(IMPORT_DIR.rglob("*"), reverse=True):  # tidy empty sub-folders
        if d.is_dir() and not any(d.iterdir()):
            try:
                d.rmdir()
            except OSError:
                pass
    return added


def start_import_watcher():
    def loop():
        while True:
            try:
                scan_import_folder()
            except Exception:  # noqa: BLE001
                pass
            time.sleep(IMPORT_SCAN_SECONDS)
    if IMPORT_SCAN_SECONDS > 0:
        threading.Thread(target=loop, daemon=True, name="import-watcher").start()


# ---------------------------------------------------------------- email

def smtp_settings():
    return {k: db.get_setting(k, v) for k, v in SMTP_DEFAULTS.items()}


def email_ready():
    s = smtp_settings()
    return bool(s["smtp_host"] and s["smtp_from"])


def _smtp_send(msg):
    s = smtp_settings()
    if not (s["smtp_host"] and s["smtp_from"]):
        raise RuntimeError("Email isn't set up yet. An admin can set it up in Settings.")
    port, sec = int(s["smtp_port"] or 587), (s["smtp_security"] or "starttls").lower()
    server = smtplib.SMTP_SSL(s["smtp_host"], port, timeout=60) if sec == "ssl" \
        else smtplib.SMTP(s["smtp_host"], port, timeout=60)
    with server:
        if sec == "starttls":
            server.starttls()
        if s["smtp_user"]:
            server.login(s["smtp_user"], s["smtp_password"] or "")
        msg["From"] = s["smtp_from"]
        server.send_message(msg)


def send_to_kindle(book, user):
    to = (user.get("kindle_email") or "").strip()
    if not to:
        raise RuntimeError("Add your Kindle email address in Settings first")
    fmt = next((f for f in SEND_PREFERENCE if f in book["files"]), None)
    if not fmt:
        raise RuntimeError("This book has no version Amazon accepts by email")
    path = book_dir(book) / book["files"][fmt]
    if path.stat().st_size > SEND_LIMIT_BYTES:
        raise RuntimeError("This book is over Amazon's 50 MB email limit. "
                           "Use the Kindle browser page or USB instead.")
    msg = EmailMessage()
    msg["To"] = to
    msg["Subject"] = book["title"] or "Book"
    msg.set_content(f"{book['title']}\n\nSent from your home library.")
    ctype = "application/epub+zip" if fmt == "epub" else (mimetypes.guess_type(path.name)[0]
                                                         or "application/octet-stream")
    main, sub = ctype.split("/", 1)
    msg.add_attachment(path.read_bytes(), maintype=main, subtype=sub, filename=path.name)
    _smtp_send(msg)
    db.set_user_book(user["id"], book["id"], sent_at=time.time())


def send_test_email(to):
    msg = EmailMessage()
    msg["To"] = to
    msg["Subject"] = "Kindle Shelf test email"
    msg.set_content("Email from your home library is working.")
    _smtp_send(msg)
