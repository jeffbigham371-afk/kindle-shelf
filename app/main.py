"""Kindle Shelf: a home ebook library that sends books to your Kindle."""

import html
import shutil
import time
from pathlib import Path
from urllib.parse import quote, urlparse

import httpx
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel

from . import audio, auth, db, library
from .auth import admin_user, current_user
from .config import (
    GUTENDEX, IMPORT_DIR, INCOMING, INPUT_EXTS, LIBRARY, OUTPUT_FORMATS, OUTPUT_PROFILE, USER_AGENT,
)

db.migrate()
audio.migrate()
auth.bootstrap_admin()

app = FastAPI(title="Kindle Shelf")
STATIC = Path(__file__).parent / "static"


@app.on_event("startup")
def _startup():
    library.refresh_old_books()
    library.start_import_watcher()


# ---------------------------------------------------------------- pages

def page(title, body):
    return HTMLResponse(
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{html.escape(title)}</title><style>"
        "body{font-family:Georgia,serif;background:#f7f3ec;color:#2a2622;margin:0;padding:24px 16px}"
        ".box{max-width:380px;margin:8vh auto;background:#fff;border:1px solid #e4dccf;border-radius:12px;padding:28px}"
        "h1{margin:0 0 4px;font-size:28px}p{color:#6d655b}label{display:block;margin:14px 0 4px;font:14px system-ui}"
        "input{width:100%;box-sizing:border-box;padding:10px;font-size:16px;border:1px solid #d8cfc1;border-radius:8px}"
        "button{margin-top:20px;width:100%;padding:12px;font-size:16px;border:0;border-radius:8px;background:#7a3e2b;color:#fff}"
        ".err{color:#a12a1b;font:14px system-ui}ul{padding-left:20px}li{margin:0 0 14px;font-size:18px}"
        "a{color:#7a3e2b}small{color:#6d655b}h2{font-size:20px;margin:28px 0 10px}"
        "</style></head><body>" + body + "</body></html>")


def safe_next(nxt):
    return nxt if nxt and nxt.startswith("/") and not nxt.startswith("//") else "/"


@app.get("/login", response_class=HTMLResponse)
def login_page(next: str = "/", error: str = ""):
    nxt = html.escape(safe_next(next))
    err = f"<p class='err'>{html.escape(error)}</p>" if error else ""
    if auth.user_count() == 0:
        return page("Set up Kindle Shelf", f"""<form class='box' method='post' action='/setup'>
            <h1>Welcome</h1><p>Create the first account. This one will be the admin.</p>{err}
            <label>Your name</label><input name='name' required>
            <label>Username</label><input name='username' autocapitalize='none' required>
            <label>Password</label><input name='password' type='password' minlength='6' required>
            <button>Create account</button></form>""")
    return page("Log in · Kindle Shelf", f"""<form class='box' method='post' action='/login'>
        <h1>Our Library</h1><p>Log in to see the bookshelf.</p>{err}
        <input type='hidden' name='next' value='{nxt}'>
        <label>Username</label><input name='username' autocapitalize='none' autocomplete='username' required>
        <label>Password</label><input name='password' type='password' autocomplete='current-password' required>
        <button>Log in</button></form>""")


@app.post("/login")
def login_submit(request: Request, username: str = Form(...), password: str = Form(...),
                 next: str = Form("/")):
    ip = request.client.host if request.client else "?"
    nxt = safe_next(next)
    if auth.throttled(ip):
        return RedirectResponse(f"/login?next={quote(nxt)}&error=" + quote("Too many tries. Wait 10 minutes."), 303)
    token = auth.login(username, password)
    if not token:
        auth.record_fail(ip)
        return RedirectResponse(f"/login?next={quote(nxt)}&error=" + quote("Wrong username or password"), 303)
    resp = RedirectResponse(nxt, 303)
    auth.set_cookie(resp, token, request)
    return resp


@app.post("/setup")
def setup_submit(request: Request, name: str = Form(...), username: str = Form(...),
                 password: str = Form(...)):
    if auth.user_count() > 0:
        raise HTTPException(403, "Already set up")
    try:
        auth.create_user(username, password, name=name, is_admin=True)
    except ValueError as e:
        return RedirectResponse("/login?error=" + quote(str(e)), 303)
    resp = RedirectResponse("/", 303)
    auth.set_cookie(resp, auth.login(username, password), request)
    return resp


@app.get("/logout")
def logout(request: Request):
    auth.logout(request.cookies.get(auth.COOKIE, ""))
    resp = RedirectResponse("/login", 303)
    resp.delete_cookie(auth.COOKIE)
    return resp


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    if not auth.user_from_request(request):
        return RedirectResponse("/login", 303)
    return HTMLResponse((STATIC / "index.html").read_text(encoding="utf-8"))


@app.get("/kindle", response_class=HTMLResponse)
def kindle_page(request: Request):
    """Plain page for the Kindle's own web browser: tap a title to download it to the device."""
    user = auth.user_from_request(request)
    if not user:
        return RedirectResponse("/login?next=/kindle", 303)
    books = [b for b in db.list_books(user["id"]) if b["status"] == "done" and "azw3" in b["files"]]

    def items(bs):
        return "".join(
            f"<li><a href='/books/{b['id']}/file/azw3'>{html.escape(b['title'] or 'Untitled')}</a>"
            f"<br><small>{html.escape(b['author'] or '')}</small></li>" for b in bs) or "<li><small>None</small></li>"
    reading = [b for b in books if b.get("read_status") == "reading"]
    want = [b for b in books if b.get("read_status") == "want"]
    body = f"<h1>{html.escape(user['name'] or 'My')}'s books</h1><p>Tap a title to download it to this Kindle.</p>"
    if reading:
        body += f"<h2>Reading</h2><ul>{items(reading)}</ul>"
    if want:
        body += f"<h2>Want to read</h2><ul>{items(want)}</ul>"
    body += f"<h2>Whole library</h2><ul>{items(books)}</ul>"
    return page("Kindle Shelf", body)


# ---------------------------------------------------------------- account

class MeUpdate(BaseModel):
    name: str | None = None
    kindle_email: str | None = None
    auto_send: bool | None = None
    current_password: str | None = None
    new_password: str | None = None


@app.get("/api/me")
def me(user=Depends(current_user)):
    return auth.public_user(user)


@app.put("/api/me")
def update_me(req: MeUpdate, user=Depends(current_user)):
    if req.name is not None:
        db.x("UPDATE users SET name=? WHERE id=?", (req.name.strip() or user["username"], user["id"]))
    if req.kindle_email is not None:
        email = req.kindle_email.strip()
        if email and "@" not in email:
            raise HTTPException(400, "That doesn't look like an email address")
        db.x("UPDATE users SET kindle_email=? WHERE id=?", (email or None, user["id"]))
    if req.auto_send is not None:
        db.x("UPDATE users SET auto_send=? WHERE id=?", (int(req.auto_send), user["id"]))
    if req.new_password:
        if not auth.check_password(req.current_password or "", user["pw_hash"]):
            raise HTTPException(400, "Your current password is wrong")
        if len(req.new_password) < 6:
            raise HTTPException(400, "Password must be at least 6 characters")
        db.x("UPDATE users SET pw_hash=? WHERE id=?", (auth.hash_password(req.new_password), user["id"]))
    return auth.public_user(db.q("SELECT * FROM users WHERE id=?", (user["id"],), one=True))


# ---------------------------------------------------------------- admin

class NewUser(BaseModel):
    username: str
    password: str
    name: str | None = None
    kindle_email: str | None = None
    is_admin: bool = False


class UserUpdate(BaseModel):
    password: str | None = None
    is_admin: bool | None = None


class EmailSettings(BaseModel):
    smtp_host: str | None = None
    smtp_port: str | None = None
    smtp_security: str | None = None
    smtp_user: str | None = None
    smtp_password: str | None = None  # empty = keep the saved one
    smtp_from: str | None = None


@app.get("/api/users")
def list_users(_=Depends(admin_user)):
    return [auth.public_user(u) for u in db.q("SELECT * FROM users ORDER BY id")]


@app.post("/api/users")
def add_user(req: NewUser, _=Depends(admin_user)):
    try:
        uid = auth.create_user(req.username, req.password, req.name, req.kindle_email, req.is_admin)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return auth.public_user(db.q("SELECT * FROM users WHERE id=?", (uid,), one=True))


@app.put("/api/users/{uid}")
def update_user(uid: int, req: UserUpdate, admin=Depends(admin_user)):
    if req.password:
        if len(req.password) < 6:
            raise HTTPException(400, "Password must be at least 6 characters")
        db.x("UPDATE users SET pw_hash=? WHERE id=?", (auth.hash_password(req.password), uid))
        db.x("DELETE FROM sessions WHERE user_id=?", (uid,))
    if req.is_admin is not None and uid != admin["id"]:
        db.x("UPDATE users SET is_admin=? WHERE id=?", (int(req.is_admin), uid))
    return {"ok": True}


@app.delete("/api/users/{uid}")
def delete_user(uid: int, admin=Depends(admin_user)):
    if uid == admin["id"]:
        raise HTTPException(400, "You can't delete your own account")
    for sql in ("DELETE FROM sessions WHERE user_id=?", "DELETE FROM user_books WHERE user_id=?",
                "DELETE FROM users WHERE id=?"):
        db.x(sql, (uid,))
    return {"ok": True}


@app.get("/api/settings/email")
def get_email_settings(_=Depends(admin_user)):
    s = library.smtp_settings()
    s["smtp_password"] = ""
    s["has_password"] = bool(library.smtp_settings()["smtp_password"])
    return s


@app.put("/api/settings/email")
def save_email_settings(req: EmailSettings, _=Depends(admin_user)):
    for k, v in req.model_dump().items():
        if k == "smtp_password" and not v:
            continue
        if v is not None:
            db.set_setting(k, v.strip())
    return {"ok": True}


@app.post("/api/settings/email/test")
def test_email(user=Depends(admin_user)):
    to = library.smtp_settings()["smtp_from"]  # send to ourselves
    try:
        library.send_test_email(to)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"Sending failed: {e}")
    return {"ok": True, "to": to}


@app.post("/api/import/scan")
def scan_import(user=Depends(admin_user)):
    return {"added": library.scan_import_folder(user)}


# ---------------------------------------------------------------- library

@app.get("/api/config")
def config(user=Depends(current_user)):
    return {
        "email_ready": library.email_ready(),
        "send_from": library.smtp_settings()["smtp_from"],
        "formats": OUTPUT_FORMATS,
        "profile": OUTPUT_PROFILE,
        "calibre": shutil.which("ebook-convert") is not None,
        "library_dir": str(LIBRARY) if user["is_admin"] else None,
        "import_dir": str(IMPORT_DIR) if user["is_admin"] else None,
    }


def present(b, users=None):
    users = users or {}
    adder = users.get(b.get("added_by"))
    return {
        "id": b["id"], "status": b["status"], "message": b["message"], "created": b["created"],
        "title": b["title"] or b["source"], "author": b["author"], "description": b.get("description"),
        "series": b.get("series"), "series_index": b.get("series_index"),
        "published": b.get("published"), "language": b.get("language"), "tags": b.get("tags") or [],
        "has_cover": bool(b.get("cover")), "formats": [f for f in b["files"] if f != "original"],
        "source": b["source"], "added_by": adder, "added_by_id": b.get("added_by"),
        "read_status": b.get("read_status"), "favorite": bool(b.get("favorite")),
        "sent_at": b.get("my_sent_at"),
        "cover_v": int(b.get("meta_version") or 0),
    }


def user_names():
    return {u["id"]: u["name"] for u in db.q("SELECT id, name FROM users")}


@app.get("/api/books")
def books(user=Depends(current_user)):
    names = user_names()
    return [present(b, names) for b in db.list_books(user["id"])]


def load_book(book_id, user):
    b = next((b for b in db.list_books(user["id"]) if b["id"] == book_id), None)
    if not b:
        raise HTTPException(404, "Book not found")
    return b


@app.get("/api/books/{book_id}")
def book(book_id: str, user=Depends(current_user)):
    return present(load_book(book_id, user), user_names())


class AddBook(BaseModel):
    url: str | None = None
    gutenberg_id: int | None = None
    send: bool | None = None


def gutendex_book(b):
    fmts = b.get("formats", {})
    epub = next((u for t, u in fmts.items() if t.startswith("application/epub+zip")), None)
    summaries = b.get("summaries") or []
    return {
        "id": b["id"], "title": b.get("title"),
        "authors": " & ".join(library.flip_name(a["name"]) for a in b.get("authors", [])),
        "languages": b.get("languages", []), "downloads": b.get("download_count"),
        "cover": fmts.get("image/jpeg"), "epub": epub,
        "summary": summaries[0] if summaries else None,
    }


@app.get("/api/search")
def search(q: str, page: int = 1, user=Depends(current_user)):
    try:
        r = httpx.get(f"{GUTENDEX}/books", params={"search": q, "page": page}, timeout=30,
                      headers={"User-Agent": USER_AGENT}, follow_redirects=True)
        r.raise_for_status()
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Couldn't reach Project Gutenberg: {e}")
    data = r.json()
    have = {b["source"]: b["id"] for b in db.q("SELECT id, source FROM books WHERE status != 'error'")}
    results = []
    for b in data.get("results", []):
        g = gutendex_book(b)
        g["in_library"] = have.get(f"Gutenberg #{g['id']}")
        results.append(g)
    return {"count": data.get("count", 0), "next": bool(data.get("next")), "results": results}


@app.post("/api/books")
def add_book(req: AddBook, user=Depends(current_user)):
    send = bool(user["auto_send"]) if req.send is None else req.send
    if req.gutenberg_id:
        existing = db.q("SELECT id FROM books WHERE source=? AND status != 'error'",
                        (f"Gutenberg #{req.gutenberg_id}",), one=True)
        if existing:
            return {"id": existing["id"], "existing": True}
        r = httpx.get(f"{GUTENDEX}/books/{req.gutenberg_id}", timeout=30,
                      headers={"User-Agent": USER_AGENT}, follow_redirects=True)
        if r.status_code != 200:
            raise HTTPException(404, "Couldn't find that book on Project Gutenberg")
        g = gutendex_book(r.json())
        if not g["epub"]:
            raise HTTPException(400, "Project Gutenberg has no EPUB for that book")
        book_id = library.queue_book(url=g["epub"], user=user, send=send, source=f"Gutenberg #{g['id']}",
                                     title=g["title"], author=g["authors"] or None,
                                     cover_url=g["cover"], description=g["summary"])
    elif req.url:
        if urlparse(req.url).scheme not in ("http", "https"):
            raise HTTPException(400, "The link must start with http:// or https://")
        book_id = library.queue_book(url=req.url, user=user, send=send, source=req.url)
    else:
        raise HTTPException(400, "Choose a book or paste a link")
    return {"id": book_id}


@app.post("/api/books/upload")
async def upload(file: UploadFile = File(...), send: bool = Form(False), user=Depends(current_user)):
    ext = Path(file.filename or "").suffix.lower()
    if ext not in INPUT_EXTS:
        raise HTTPException(400, f"Can't use '{ext or 'that'}' files. Try EPUB, MOBI, AZW3, PDF or DOCX.")
    tmp = INCOMING / ("upload-" + library.uuid.uuid4().hex[:8])
    tmp.mkdir(parents=True, exist_ok=True)
    dest = tmp / (library.safe_name(Path(file.filename).stem, "book") + ext)
    with open(dest, "wb") as f:
        shutil.copyfileobj(file.file, f)
    book_id = library.queue_book(local_path=str(dest), user=user, send=send,
                                 source=f"Uploaded by {user['name']}: {file.filename}")
    return {"id": book_id}


class BookEdit(BaseModel):
    title: str | None = None
    author: str | None = None
    series: str | None = None
    series_index: float | None = None
    description: str | None = None


def _apply_edits(book_id):
    library.relocate(book_id)
    library.write_metadata(db.get_book(book_id))


@app.patch("/api/books/{book_id}")
def edit_book(book_id: str, req: BookEdit, user=Depends(current_user)):
    b = load_book(book_id, user)
    if b["status"] != "done":
        raise HTTPException(409, "Wait until the book is ready")
    fields = {k: (v.strip() if isinstance(v, str) else v) for k, v in req.model_dump(exclude_unset=True).items()}
    if "title" in fields and not fields["title"]:
        raise HTTPException(400, "Title can't be empty")
    for k in ("author", "series", "description"):
        if k in fields and not fields[k]:
            fields[k] = None
    db.update_book(book_id, **fields)
    library.executor.submit(_apply_edits, book_id)
    return present(load_book(book_id, user), user_names())


@app.post("/api/books/{book_id}/cover")
async def set_cover(book_id: str, file: UploadFile = File(...), user=Depends(current_user)):
    b = load_book(book_id, user)
    if b["status"] != "done":
        raise HTTPException(409, "Wait until the book is ready")
    data = await file.read()
    if not (data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n"):
        raise HTTPException(400, "Covers must be JPG or PNG images")
    name = "cover.png" if data[:4] == b"\x89PNG" else "cover.jpg"
    folder = library.book_dir(b)
    for old in ("cover.jpg", "cover.png"):
        (folder / old).unlink(missing_ok=True)
    (folder / name).write_bytes(data)
    db.update_book(book_id, cover=name, meta_version=int(b.get("meta_version") or 0) + 1)
    library.executor.submit(library.write_metadata, db.get_book(book_id))
    return present(load_book(book_id, user), user_names())


class ReadState(BaseModel):
    read_status: str | None = None  # want | reading | finished | None
    favorite: bool | None = None


@app.put("/api/books/{book_id}/state")
def set_state(book_id: str, req: ReadState, user=Depends(current_user)):
    load_book(book_id, user)
    data = req.model_dump(exclude_unset=True)
    upd = {}
    if "read_status" in data:
        if data["read_status"] not in (None, "want", "reading", "finished"):
            raise HTTPException(400, "Unknown status")
        upd["status"] = data["read_status"]
    if "favorite" in data:
        upd["favorite"] = int(bool(data["favorite"]))
    db.set_user_book(user["id"], book_id, **upd)
    return present(load_book(book_id, user), user_names())


@app.post("/api/books/{book_id}/send")
def send_book(book_id: str, user=Depends(current_user)):
    b = load_book(book_id, user)
    if b["status"] != "done":
        raise HTTPException(409, "This book isn't ready yet")
    try:
        library.send_to_kindle(b, user)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, str(e))
    if not b.get("read_status"):
        db.set_user_book(user["id"], book_id, status="reading")
    return present(load_book(book_id, user), user_names())


@app.post("/api/books/{book_id}/retry")
def retry_book(book_id: str, user=Depends(current_user)):
    b = load_book(book_id, user)
    if b["status"] != "error":
        raise HTTPException(409, "Only failed books can be retried")
    src = b["source"] or ""
    if not src.startswith(("http://", "https://", "Gutenberg #")):
        raise HTTPException(400, "Uploaded files can't be retried. Upload the file again.")
    db.x("DELETE FROM books WHERE id=?", (book_id,))
    if src.startswith("Gutenberg #"):
        return add_book(AddBook(gutenberg_id=int(src.split("#")[1]), send=False), user)
    return add_book(AddBook(url=src, send=False), user)


@app.delete("/api/books/{book_id}")
def delete_book(book_id: str, user=Depends(current_user)):
    b = load_book(book_id, user)
    if not user["is_admin"] and b.get("added_by") != user["id"]:
        raise HTTPException(403, "Only an admin or the person who added it can remove this book")
    if b["status"] in db.BUSY:
        raise HTTPException(409, "This book is still being prepared")
    library.delete_files(b)
    db.x("DELETE FROM user_books WHERE book_id=?", (book_id,))
    db.x("DELETE FROM books WHERE id=?", (book_id,))
    return {"ok": True}


@app.get("/books/{book_id}/cover")
def cover(book_id: str, user=Depends(current_user)):
    b = db.get_book(book_id)
    if not b or not b.get("cover"):
        raise HTTPException(404)
    path = library.book_dir(b) / b["cover"]
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path, headers={"Cache-Control": "private, max-age=86400"})


@app.get("/books/{book_id}/file/{fmt}")
def book_file(book_id: str, fmt: str, user=Depends(current_user)):
    b = db.get_book(book_id)
    if not b or fmt not in b["files"]:
        raise HTTPException(404)
    path = library.book_dir(b) / b["files"][fmt]
    media = {"epub": "application/epub+zip", "azw3": "application/vnd.amazon.ebook"}.get(fmt)
    return FileResponse(path, filename=path.name, media_type=media)


# ---------------------------------------------------------------- audiobooks

class AudioSettings(BaseModel):
    audiobook_dir: str | None = None
    abs_url: str | None = None
    abs_token: str | None = None  # empty = keep the saved one
    audio_quality: str | None = None


class AudioAdd(BaseModel):
    identifier: str


def present_audio(a, names):
    return {k: a.get(k) for k in ("id", "status", "message", "title", "author", "narrator",
                                  "file_count", "size", "created", "source")} | \
        {"added_by": names.get(a.get("added_by")), "added_by_id": a.get("added_by"), "path": a.get("path")}


@app.get("/api/audiobooks")
def audiobooks(user=Depends(current_user)):
    names = user_names()
    s = audio.settings()
    return {"items": [present_audio(a, names) for a in audio.list_all()],
            "abs_url": s["abs_url"], "folder": s["audiobook_dir"] if user["is_admin"] else None}


@app.get("/api/audiobooks/search")
def audiobook_search(q: str, page: int = 1, user=Depends(current_user)):
    try:
        return audio.search(q, page)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Couldn't reach the Internet Archive: {e}")


@app.post("/api/audiobooks")
def audiobook_add(req: AudioAdd, user=Depends(current_user)):
    try:
        aid, existing = audio.queue_archive(req.identifier, user)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"id": aid, "existing": existing}


@app.post("/api/audiobooks/upload")
async def audiobook_upload(files: list[UploadFile] = File(...), title: str = Form(...),
                           author: str = Form(""), narrator: str = Form(""), user=Depends(current_user)):
    if not title.strip():
        raise HTTPException(400, "Please enter the title")
    tmp = INCOMING / ("audio-upload-" + library.uuid.uuid4().hex[:8])
    tmp.mkdir(parents=True, exist_ok=True)
    saved = []
    for f in files:
        ext = Path(f.filename or "").suffix.lower()
        if ext not in audio.AUDIO_EXTS | audio.IMAGE_EXTS | {".zip"}:
            shutil.rmtree(tmp, ignore_errors=True)
            raise HTTPException(400, f"Can't use '{f.filename}'. Use MP3, M4B, M4A, FLAC, OGG or a ZIP of them.")
        dest = tmp / (library.safe_name(Path(f.filename).stem, "file", 150) + ext)
        with open(dest, "wb") as out:
            shutil.copyfileobj(f.file, out)
        saved.append(str(dest))
    aid = audio.queue_upload(saved, title.strip(), author.strip() or None, narrator.strip() or None, user)
    return {"id": aid}


@app.delete("/api/audiobooks/{aid}")
def audiobook_forget(aid: str, user=Depends(current_user)):
    a = audio.get(aid)
    if not a:
        raise HTTPException(404)
    if a["status"] in ("queued", "downloading"):
        raise HTTPException(409, "Still downloading")
    if not user["is_admin"] and a.get("added_by") != user["id"]:
        raise HTTPException(403, "Only an admin or the person who added it can remove this")
    db.x("DELETE FROM audiobooks WHERE id=?", (aid,))
    return {"ok": True}


@app.get("/api/settings/audio")
def get_audio_settings(_=Depends(admin_user)):
    s = audio.settings()
    return {**s, "abs_token": "", "has_token": bool(s["abs_token"])}


@app.put("/api/settings/audio")
def save_audio_settings(req: AudioSettings, _=Depends(admin_user)):
    data = req.model_dump()
    if data.get("audiobook_dir"):
        try:
            data["audiobook_dir"] = audio.check_dir(data["audiobook_dir"].strip())
        except OSError as e:
            raise HTTPException(400, f"Can't write to that folder: {e}")
    if data.get("audio_quality") not in (None, "small", "high"):
        raise HTTPException(400, "Unknown quality")
    for k, v in data.items():
        if k == "abs_token" and not v:
            continue
        if v is not None:
            db.set_setting(k, v.strip())
    return get_audio_settings()


@app.post("/api/settings/audio/test")
def test_abs(_=Depends(admin_user)):
    s = audio.settings()
    if not (s["abs_url"] and s["abs_token"]):
        raise HTTPException(400, "Enter the Audiobookshelf address and API key first")
    try:
        r = httpx.get(s["abs_url"].rstrip("/") + "/api/libraries", timeout=15,
                      headers={"Authorization": f"Bearer {s['abs_token']}"})
        r.raise_for_status()
        libs = r.json().get("libraries", [])
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"Couldn't connect to Audiobookshelf: {e}")
    return {"libraries": [{"name": l.get("name"), "folders": [f.get("fullPath") for f in l.get("folders", [])]}
                          for l in libs]}



@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    # Browsers following a link while logged out get the login page, not JSON.
    if exc.status_code == 401 and not request.url.path.startswith("/api/"):
        return RedirectResponse(f"/login?next={quote(request.url.path)}", 303)
    from fastapi.responses import JSONResponse
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)
