# Kindle Shelf

A small self-hosted web app that fetches ebooks, converts them for Kindle with Calibre, and gets them onto your device.

**Get books from**
- Project Gutenberg search (built in)
- Any direct download link (EPUB, MOBI, AZW3, PDF, FB2, DOCX, TXT, HTML, CBZ…)
- A file uploaded from your computer

**Output**
- **EPUB**, cleaned up by Calibre for Amazon's *Send to Kindle* (Amazon no longer accepts MOBI)
- **AZW3**, the native Kindle format, for USB or the Kindle's web browser

**Get it onto the Kindle**
1. **Email:** click *Send to Kindle*, or set `AUTO_SEND=true`
2. **Kindle browser:** open `http://your-server:8080/kindle` in the Kindle's web browser and tap a title. The AZW3 downloads straight to the device.
3. **USB:** download the AZW3 and copy it into the Kindle's `documents/` folder

## Install

```bash
git clone <this repo> kindle-shelf && cd kindle-shelf
cp .env.example .env      # then edit it
docker compose up -d --build
```

Open `http://your-server:8080`. Books and the job database are stored in `./data`.

### Setting up Send to Kindle email
1. On amazon.com, go to **Manage Your Content and Devices → Preferences → Personal Document Settings**.
2. Copy your Kindle's address (`something@kindle.com`) into `KINDLE_EMAIL`.
3. Add the address you'll send from (`SMTP_FROM`) to the **Approved Personal Document E-mail List**.
4. Enter SMTP details. For Gmail, use `smtp.gmail.com`, port 587, and an [app password](https://myaccount.google.com/apppasswords).

Amazon rejects attachments over 50 MB. For those books, use USB or the `/kindle` page.

### Security
Set `APP_PASSWORD` to require a login (HTTP Basic auth). If the app is reachable from the internet, put it behind a reverse proxy with HTTPS (Caddy, Nginx, Traefik).

## Running without Docker
Install Calibre (`apt install calibre`), then:
```bash
pip install -r requirements.txt
QT_QPA_PLATFORM=offscreen uvicorn app.main:app --host 0.0.0.0 --port 8080
```

## Settings (`.env`)
| Variable | Default | Purpose |
|---|---|---|
| `APP_PASSWORD` | *(none)* | Turns on login when set |
| `KINDLE_PROFILE` | `kindle_pw3` | Calibre output profile (`kindle_scribe`, `kindle_oasis`, `kindle`…) |
| `OUTPUT_FORMATS` | `epub,azw3` | Formats to produce |
| `MAX_DOWNLOAD_MB` | `200` | Largest file the app will download |
| `KINDLE_EMAIL`, `SMTP_*` | | Email delivery |
| `AUTO_SEND` | `false` | Email every finished book automatically |
| `WORKERS` | `2` | Number of books converted at the same time |
