"""Settings read from environment variables (all optional)."""

import os
from pathlib import Path


def env(name, default=None):
    v = os.environ.get(name)
    return v if v not in (None, "") else default


DATA_DIR = Path(env("DATA_DIR", "./data")).resolve()
# The library is a normal folder tree on the server: Library/<Author>/<Title>/...
LIBRARY = Path(env("LIBRARY_DIR", str(DATA_DIR / "library"))).resolve()
# Drop ebook files here and they are added to the library automatically.
IMPORT_DIR = Path(env("IMPORT_DIR", str(DATA_DIR / "import"))).resolve()
INCOMING = DATA_DIR / "incoming"
DB_PATH = DATA_DIR / "kindle-shelf.db"

# First admin account is created from these if no users exist yet.
APP_USER = env("APP_USER", "admin")
APP_PASSWORD = env("APP_PASSWORD")

MAX_DOWNLOAD_MB = int(env("MAX_DOWNLOAD_MB", "200"))
CONVERT_TIMEOUT = int(env("CONVERT_TIMEOUT", "900"))
OUTPUT_PROFILE = env("KINDLE_PROFILE", "kindle_pw3")
OUTPUT_FORMATS = [f.strip().lower() for f in env("OUTPUT_FORMATS", "epub,azw3").split(",") if f.strip()]
WORKERS = int(env("WORKERS", "2"))
IMPORT_SCAN_SECONDS = int(env("IMPORT_SCAN_SECONDS", "60"))
SESSION_DAYS = int(env("SESSION_DAYS", "180"))

# Email defaults; can be overridden from the admin Settings page.
SMTP_DEFAULTS = {
    "smtp_host": env("SMTP_HOST"),
    "smtp_port": env("SMTP_PORT", "587"),
    "smtp_security": env("SMTP_SECURITY", "starttls"),
    "smtp_user": env("SMTP_USER"),
    "smtp_password": env("SMTP_PASSWORD"),
    "smtp_from": env("SMTP_FROM", env("SMTP_USER")),
}
DEFAULT_KINDLE_EMAIL = env("KINDLE_EMAIL")

GUTENDEX = env("GUTENDEX_URL", "https://gutendex.com")
USER_AGENT = "KindleShelf/2.0 (self-hosted home library)"

INPUT_EXTS = {
    ".epub", ".mobi", ".azw", ".azw3", ".prc", ".pdf", ".fb2", ".txt", ".html",
    ".htm", ".docx", ".rtf", ".odt", ".lit", ".cbz", ".cbr", ".kepub", ".pdb",
}

for d in (DATA_DIR, LIBRARY, IMPORT_DIR, INCOMING):
    d.mkdir(parents=True, exist_ok=True)
