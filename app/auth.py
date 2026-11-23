"""Accounts and login sessions."""

import hashlib
import hmac
import secrets
import time

from fastapi import HTTPException, Request

from . import db
from .config import APP_PASSWORD, APP_USER, DEFAULT_KINDLE_EMAIL, SESSION_DAYS

COOKIE = "ks_session"
_ITER = 200_000


def hash_password(pw):
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), _ITER).hex()
    return f"pbkdf2${_ITER}${salt}${h}"


def check_password(pw, stored):
    try:
        _, it, salt, h = stored.split("$")
    except (ValueError, AttributeError):
        return False
    calc = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), int(it)).hex()
    return hmac.compare_digest(calc, h)


def public_user(u):
    return {k: u[k] for k in ("id", "username", "name", "kindle_email", "is_admin", "auto_send")}


def create_user(username, password, name=None, kindle_email=None, is_admin=False):
    username = (username or "").strip()
    if not username or not password:
        raise ValueError("Username and password are required")
    if len(password) < 6:
        raise ValueError("Password must be at least 6 characters")
    if db.q("SELECT 1 FROM users WHERE username=?", (username,), one=True):
        raise ValueError("That username is taken")
    cur = db.x("INSERT INTO users (username, name, pw_hash, kindle_email, is_admin, created) "
               "VALUES (?,?,?,?,?,?)",
               (username, name or username, hash_password(password), kindle_email or None,
                int(is_admin), time.time()))
    return cur.lastrowid


def user_count():
    return db.q("SELECT COUNT(*) AS n FROM users", one=True)["n"]


def bootstrap_admin():
    """Create the first admin from APP_USER/APP_PASSWORD so existing installs keep working."""
    if user_count() == 0 and APP_PASSWORD:
        create_user(APP_USER, APP_PASSWORD, name=APP_USER.title(),
                    kindle_email=DEFAULT_KINDLE_EMAIL, is_admin=True)


def login(username, password):
    u = db.q("SELECT * FROM users WHERE username=?", ((username or "").strip(),), one=True)
    if not u or not check_password(password or "", u["pw_hash"]):
        return None
    token = secrets.token_urlsafe(32)
    now = time.time()
    db.x("INSERT INTO sessions VALUES (?,?,?,?)", (token, u["id"], now, now + SESSION_DAYS * 86400))
    db.x("DELETE FROM sessions WHERE expires < ?", (now,))
    return token


def logout(token):
    db.x("DELETE FROM sessions WHERE token=?", (token,))


def user_from_request(request: Request):
    token = request.cookies.get(COOKIE)
    if not token:
        return None
    return db.q("""SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id
                   WHERE s.token=? AND s.expires > ?""", (token, time.time()), one=True)


def set_cookie(response, token, request: Request):
    response.set_cookie(COOKIE, token, max_age=SESSION_DAYS * 86400, httponly=True,
                        samesite="lax", secure=request.url.scheme == "https")


# FastAPI dependencies
def current_user(request: Request):
    u = user_from_request(request)
    if not u:
        raise HTTPException(401, "Please log in")
    return u


def admin_user(request: Request):
    u = current_user(request)
    if not u["is_admin"]:
        raise HTTPException(403, "Only an admin can do that")
    return u


# Very small login throttle: 10 failures per IP per 10 minutes.
_fails: dict[str, list[float]] = {}


def throttled(ip):
    now = time.time()
    _fails[ip] = [t for t in _fails.get(ip, []) if now - t < 600]
    return len(_fails[ip]) >= 10


def record_fail(ip):
    _fails.setdefault(ip, []).append(time.time())
