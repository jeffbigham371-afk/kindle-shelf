# Kindle Shelf

A self-hosted home ebook library. Books are stored on your server, and each person can send them straight to their own Kindle.

- **Bookshelf** with covers, search, sorting, and filters (Reading, Want to read, Finished, Favorites)
- **Separate logins** for each person, each with their own Kindle email, reading status, and favorites
- **Send to my Kindle** emails the book to that person's Kindle (Amazon's Send to Kindle), or automatically when it's added
- **Adding books**: search free Project Gutenberg classics, upload files, paste a link, or drop files in the server's **import folder**
- **Book pages** with description and series, plus editing of titles, authors, and covers (saved into the book files too)
- **Kindle browser page** at `/kindle`: tap a title on the Kindle to download it directly
- **Audiobooks for Audiobookshelf**: search free LibriVox and Internet Archive audiobooks, or upload DRM-free MP3/M4B files. They're saved into your Audiobookshelf library folder as `Author/Title/` with `cover.jpg`, `desc.txt` and `reader.txt`.

### Where the library lives
Everything stays on the server:
```
data/
  library/<Author>/<Title>/<Title> - <Author>.epub   (sent to Kindle by email)
                          /<Title> - <Author>.azw3   (Kindle browser / USB)
                          /original.<ext>            (the file as it was added)
                          /cover.jpg
  import/          drop ebook files here to add them automatically
  kindle-shelf.db  accounts, reading lists, settings
```
Back up the whole `data` folder.

### First login
If `APP_PASSWORD` is set, an admin account is created from `APP_USER` / `APP_PASSWORD` on first start. Otherwise the first visit asks you to create one. As admin, go to **Settings** to:
- add other people's accounts
- set up the email account the library sends from (Gmail works with an app password), then click **Send a test email**

Each person then enters their Kindle email under **Settings**. In their Amazon account, they add the library's sending address to the **Approved Personal Document E-mail List**.

### Audiobooks
As admin, open **Settings → Audiobookshelf** and set **Audiobook folder** to the folder your Audiobookshelf library already watches. Audiobookshelf picks new books up on its own. For new books to appear instantly, also enter the Audiobookshelf address and an API token (Audiobookshelf → Settings → Users → your user). Each download is assembled in a staging folder and moved in only when complete, so Audiobookshelf never sees a half-downloaded book.

If that folder is a network share on another machine, use its UNC path (`\\nas\audiobooks`). The account running Kindle Shelf must be allowed to write there. The built-in SYSTEM account usually can't reach network shares.

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
