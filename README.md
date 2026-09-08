# Fanficthing

A self-hosted web app for saving and reading fanfiction and web novels offline. Paste an [Archive of Our Own (AO3)](https://archiveofourown.org) or [novelbin](https://www.novelbin.cc) link, or drop in an EPUB, and Fanficthing stores it in a local library you can read anytime.

## Features

- **Save fics locally** — download any AO3 work or novelbin novel by URL into a local SQLite database
- **EPUB import** — drag and drop `.epub` files anywhere on the library page (or use the EPUB button) to read them alongside your fics, images included
- **Built-in reader** — clean reading view with chapter navigation, in-text search, font and theme controls, and keyboard shortcuts
- **Reading progress** — automatically tracks your current chapter and scroll position
- **Library search and sort** — search by title, author, fandom, or tags; sort by date, title, author, or word count
- **Update checking** — check a single fic or your whole library for new chapters; the reader also checks on open
- **LAN sharing** — pull fics from another Fanficthing on your network, protected by a per-instance token
- **Wallpaper theming** — on Arch Linux with [HyDE](https://github.com/HyDE-Project/HyDE) wallbash, the UI (including the background orbs) recolors itself live to match your wallpaper

## Requirements

- Python 3.12+

## Installation

1. Clone the repository:
   ```bash
   git clone https://github.com/NeviSenpai111/FanficThing.git
   cd FanficThing
   ```

2. Run the setup script:
   ```bash
   python setup.py
   ```
   This will:
   - Create a Python virtual environment (`.venv`)
   - Install all dependencies
   - Install the Playwright Chromium browser (used for AO3 scraping)
   - Create the `data/` directory

   Works on Linux, macOS, and Windows.

### Updating

After pulling a new version, refresh the dependencies and browser:

```bash
.venv/bin/pip install -r requirements.txt --upgrade
.venv/bin/playwright install chromium
```

(On Windows use `.venv\Scripts\pip.exe` and `.venv\Scripts\playwright.exe`.)

## Usage

**Linux / macOS:**
```bash
./start.sh
```

**Windows:**
```
Double-click start.bat (created by setup.py)
```

**Or run manually:**
```bash
# Linux/macOS
.venv/bin/uvicorn app:app --host 0.0.0.0 --port 8000

# Windows
.venv\Scripts\uvicorn.exe app:app --host 0.0.0.0 --port 8000
```

Then open [http://localhost:8000](http://localhost:8000) in your browser.

### Reading on your phone

Any device on the same Wi-Fi can use the library. Click Share on the library page: it shows the address to type into your phone's browser and a QR code you can scan instead. The laptop has to stay awake while you read, so make sure it does not suspend when idle or when the lid is closed.

### Adding a fic

Paste an AO3 work URL (e.g. `https://archiveofourown.org/works/12345`) or a novelbin book URL (e.g. `https://www.novelbin.cc/book/some-novel`) into the input field and click Download. The fic is fetched in the background and appears in your library when ready. Long novels are saved chapter by chapter, so an interrupted download keeps what it already fetched.

### Adding an EPUB

Drop one or more `.epub` files anywhere on the library page, or click the EPUB button to pick them. Chapters, images, and metadata are imported into the library. Re-uploading the same file updates the existing entry instead of duplicating it.

### Reading

Click any fic in your library to open the reader. Chapters are loaded one at a time as you scroll, so even very long novels open instantly. Your reading position is saved automatically. In-text search covers the chapters loaded so far.

| Key | Action |
| --- | --- |
| `←` / `→` | Previous / next chapter |
| `T` | Toggle chapter list |
| `B` | Save / return to bookmark |
| `Ctrl`+`F` | Search in text |
| `?` | Show keyboard shortcuts |
| `Esc` | Close panels |

On the library page, `Ctrl`+`K` focuses the search box.

### Sharing over the network

Click Share on the library page to see your address and token. Give both to someone running Fanficthing on the same network, and they can browse and import your fics from their own Share dialog. The token lives in `data/share_token.txt` and is generated on first start.

## Tech Stack

- **[FastAPI](https://fastapi.tiangolo.com/)** — async web framework
- **[SQLite](https://www.sqlite.org/)** — local database (stored in `data/fanfics.db`)
- **[Playwright](https://playwright.dev/python/)** — headless browser for AO3 scraping
- **[curl_cffi](https://github.com/lexiforest/curl_cffi)** — browser-impersonating HTTP client for novelbin
- **[Jinja2](https://jinja.palletsprojects.com/)** — HTML templating
- **[Beautiful Soup](https://www.crummy.com/software/BeautifulSoup/)** — HTML parsing

## License

This project is for personal use. Please respect the Terms of Service of AO3 and any other site you download from when using this tool.
