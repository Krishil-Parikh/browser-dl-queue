# browser-dl-queue

A small TUI download queue driven by a real browser (Playwright).
Point it at a page, it lists the links, you remove what you don't want,
then it visits each link, clicks the download button you specify, and keeps
N downloads (default 3) running at once. A new one starts the moment a slot frees up.

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows  (source .venv/bin/activate elsewhere)
pip install -r requirements.txt
playwright install chromium
python app.py
```

## Flow

1. **Source page URL** + **Link filter** (regex) → click **① Fetch links**.
   The page is opened in Chromium, JS is allowed to render, and every link
   (`<a href>` plus plain-text URLs) matching the filter is listed. Nothing downloads yet.
2. **Review.** Click **✕** on a row to drop it, or tick rows with **☐** and use the toolbar.
3. Click **② ▶ Continue**. At most *slots* jobs run; the rest wait. Jobs are started
   one at a time with a random gap between starts (`start_gap_min_s`–`start_gap_max_s`
   in `config.json`, default 10–20 s); the status bar shows the countdown.
4. While running, each row's **⏸ / ▶ / ↻** pauses, resumes or retries it, and **✕** removes it.

Paused or interrupted files resume from where they stopped (`.part` files + HTTP Range).
The queue is saved to `state.json`, so quitting and reopening picks up where you left off.

## Two modes

| Download button selector | What happens per link |
| --- | --- |
| *(empty)* | The link **is** the file; it is fetched directly. |
| e.g. `#download`, `button:has-text("Download")` | The link's page is opened, the element is clicked, the browser's download is intercepted, and the transfer is handed to an HTTP client (same cookies / user agent / referer) so you get live progress and resume. Popups opened by the click are closed automatically, and the click is retried if no download starts. |

## Getting the filter right

Without a filter you'll get every link on the page (nav, footer, help pages...).
Fetch, look at the list, then tighten the regex and fetch again. Examples:

- `\.iso$` - only ISO files
- `^https://files\.example\.com/` - only links to one host
- `/p[0-9]+\.html` - only numbered part pages

## Controls

Everything is clickable. Keyboard equivalents:

| Key | Action |
| --- | --- |
| `f` | Fetch links |
| `s` | Continue / Stop starting new downloads |
| `space` | Tick row |
| `ctrl+a` | Tick all |
| `p` | Pause/resume ticked (or highlighted) |
| `delete` / `x` | Delete ticked (or highlighted) |
| `[` `]` | Move highlighted up / down |
| `+` `-` | More / fewer parallel slots |
| `a` | Paste URLs manually |
| `h` | Toggle headless browser |
| `esc` | Back to the table |
| `q` | Save and quit |

Other settings (timeouts, retries, render wait, popup closing) live in `config.json`.

## Files

| File | Job |
| --- | --- |
| `app.py` | The TUI |
| `jobs.py` | Queue, job states, scheduler, retries |
| `browser.py` | Playwright: link extraction and download-button resolution |
| `downloader.py` | Resumable HTTP transfer with progress |
| `config.py` | `config.json` settings |

Use it only for files you have the right to download.
