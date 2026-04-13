# Fanficthing

A self-hosted web app for saving and reading fanfiction from [Archive of Our Own (AO3)](https://archiveofourown.org). Paste an AO3 link, and Fanficthing downloads the fic to a local library you can read anytime — even offline.

## Features

- **Save fics locally** — download any AO3 work by URL into a local SQLite database
- **Built-in reader** — read saved fics in a clean web interface with chapter navigation
- **Reading progress** — automatically tracks your current chapter and scroll position
- **Library search** — search your saved fics by title, author, fandom, or tags
- **Update checking** — check for new chapters on fics you've already saved
- **Playwright scraper** — downloads fics directly from AO3 using a headless browser

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

### Adding a fic

Paste an AO3 work URL (e.g. `https://archiveofourown.org/works/12345`) into the input field and click Add. The fic will be downloaded in the background and appear in your library when ready.

### Reading

Click any fic in your library to open the reader. Your reading position is saved automatically.

## Tech Stack

- **[FastAPI](https://fastapi.tiangolo.com/)** — async web framework
- **[SQLite](https://www.sqlite.org/)** — local database (stored in `data/fanfics.db`)
- **[Playwright](https://playwright.dev/python/)** — headless browser for AO3 scraping
- **[Jinja2](https://jinja.palletsprojects.com/)** — HTML templating
- **[Beautiful Soup](https://www.crummy.com/software/BeautifulSoup/)** — HTML parsing

## License

This project is for personal use. Please respect AO3's Terms of Service when using this tool.
