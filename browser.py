"""Everything that touches Playwright lives here.

Two jobs:
  1. extract_links(url)  -> read every link from a (possibly JS-rendered) page
  2. resolve(url)        -> open a link's page, click the download button, and
                            capture the real file URL + cookies so the actual
                            transfer can run over HTTP with live progress.

If a "verify you are human" page shows up, everything is put on hold until
you complete it yourself in the browser window.
"""
import asyncio
import re
import time
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from urllib.parse import unquote, urlparse

from playwright.async_api import async_playwright

URL_IN_TEXT = re.compile(r"https?://[^\s<>\"']+")
CHALLENGE_TEXT = re.compile(
    r"verify (that )?you('| a)re (a )?human|are you (a )?(robot|human)|just a moment|"
    r"checking (if the site connection is secure|your browser)|attention required|"
    r"press (and|&) hold|complete the security check|i'?m not a robot",
    re.I,
)
CHALLENGE_FRAMES = ("challenges.cloudflare.com", "hcaptcha.com", "/recaptcha/", "captcha")


class HumanCheckNeeded(Exception):
    """A human check appeared but the browser is headless, so nobody can solve it."""


@dataclass
class Resolved:
    url: str
    filename: str
    headers: dict = field(default_factory=dict)
    cookies: dict = field(default_factory=dict)


def filename_from_url(url: str) -> str:
    name = PurePosixPath(unquote(urlparse(url).path)).name
    return name or "download"


class Browser:
    def __init__(self, cfg, log=print):
        self.cfg = cfg
        self.log = log
        self.on_challenge = lambda url: None  # set by the Manager
        self.verifying_url = ""               # non-empty while waiting for you
        self._pw = None
        self._browser = None
        self.context = None
        self._start_lock = asyncio.Lock()
        self._verify_lock = asyncio.Lock()
        self._clear = asyncio.Event()          # set = no verification in progress
        self._clear.set()

    @property
    def verifying(self) -> bool:
        return bool(self.verifying_url)

    async def start(self):
        async with self._start_lock:
            if self.context:
                return
            self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.launch(headless=self.cfg.headless)
            self.context = await self._browser.new_context(accept_downloads=True)
            self.log("browser started")

    async def stop(self):
        for closer in (self.context, self._browser):
            if closer:
                try:
                    await closer.close()
                except Exception:
                    pass
        if self._pw:
            await self._pw.stop()
        self.context = self._browser = self._pw = None

    async def _new_page(self):
        await self._clear.wait()  # never open new pages while you're verifying
        await self.start()
        page = await self.context.new_page()
        if self.cfg.close_popups:
            page.on("popup", lambda p: asyncio.ensure_future(p.close()))
        return page

    async def _cookies(self, url):
        return {c["name"]: c["value"] for c in await self.context.cookies(url)}

    # ---------- human checks ----------
    async def is_challenge(self, page) -> bool:
        try:
            if any(s in f.url for f in page.frames[1:] for s in CHALLENGE_FRAMES):
                return True
            title = await page.title()
            text = (await page.inner_text("body", timeout=2000))[:4000]
        except Exception:
            return False
        return bool(CHALLENGE_TEXT.search(title) or CHALLENGE_TEXT.search(text))

    async def _wait_for_human(self, page, is_done):
        """Hold everything until is_done() is true (you solved the check and the real page loaded)."""
        async with self._verify_lock:  # one verification at a time; others queue up here
            if await is_done():
                return
            if self.cfg.headless:
                raise HumanCheckNeeded(page.url)
            self.verifying_url = page.url
            self._clear.clear()
            self.log(f"[b red]human check[/] on {page.url} - complete it in the browser window")
            self.on_challenge(page.url)
            try:
                await page.bring_to_front()
                deadline = time.monotonic() + self.cfg.verify_timeout_s
                while time.monotonic() < deadline:
                    if await is_done():
                        self.log("[green]verification complete[/], continuing")
                        return
                    await asyncio.sleep(1)
                raise RuntimeError(f"human check not completed within {self.cfg.verify_timeout_s}s")
            finally:
                self.verifying_url = ""
                self._clear.set()

    async def _goto(self, page, url, ready_selector: str):
        """Open url and wait until ready_selector is present, pausing for a human check if one appears."""
        timeout = self.cfg.timeout_s * 1000
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
        if not ready_selector:
            return

        async def ready():
            return await page.query_selector(ready_selector) is not None and not await self.is_challenge(page)

        # poll for "page ready" or "human check", whichever shows first
        deadline = time.monotonic() + self.cfg.timeout_s
        while time.monotonic() < deadline:
            if await self.is_challenge(page):
                await self._wait_for_human(page, ready)
                return
            if await page.query_selector(ready_selector) is not None:
                return
            await asyncio.sleep(0.5)
        raise TimeoutError(f"{ready_selector!r} did not appear on {url} within {self.cfg.timeout_s}s")

    # ---------- the two jobs ----------
    async def extract_links(self, url: str) -> list[dict]:
        """Return [{text, url}] for links matching cfg.link_pattern, de-duplicated, in page order."""
        page = await self._new_page()
        try:
            await self._goto(page, url, self.cfg.wait_for_selector or "body")
            await page.wait_for_timeout(self.cfg.render_wait_ms)
            anchors = await page.eval_on_selector_all(
                "a[href]", "els => els.map(a => ({text: a.innerText.trim(), url: a.href}))"
            )
            # also catch plain-text URLs that aren't wrapped in <a>
            body_text = await page.inner_text("body")
        finally:
            await page.close()

        found = anchors + [{"text": "", "url": u.rstrip(".,);")} for u in URL_IN_TEXT.findall(body_text)]
        rx = re.compile(self.cfg.link_pattern)
        seen, out = set(), []
        for link in found:
            u = link["url"]
            if u in seen or not rx.search(u):
                continue
            seen.add(u)
            out.append(link)
        return out

    async def resolve(self, url: str) -> Resolved:
        """Turn a link into a downloadable file URL (+ the session needed to fetch it)."""
        await self.start()
        selector = self.cfg.download_selector.strip()
        if not selector:  # direct file links
            await self._clear.wait()
            return Resolved(url, filename_from_url(url), cookies=await self._cookies(url))

        page = await self._new_page()
        downloads = []
        page.on("download", lambda d: downloads.append(d))
        try:
            await self._goto(page, url, selector)
            for attempt in range(1, self.cfg.click_attempts + 1):
                await page.click(selector, timeout=self.cfg.timeout_s * 1000)
                for _ in range(30):  # up to 15 s for the download to start
                    if downloads:
                        break
                    await asyncio.sleep(0.5)
                if downloads:
                    break
                if await self.is_challenge(page):  # a check appeared after the click
                    async def ready():
                        if downloads:
                            return True
                        return not await self.is_challenge(page) and await page.query_selector(selector) is not None
                    await self._wait_for_human(page, ready)
                    if downloads:
                        break
                    continue
                self.log(f"click {attempt} on {selector!r} started no download, retrying")
            if not downloads:
                raise RuntimeError(f"clicking {selector!r} never started a download")

            # hand the transfer over to our own HTTP client (progress + resume)
            download = downloads[0]
            file_url, name = download.url, download.suggested_filename
            await download.cancel()
            headers = {
                "User-Agent": await page.evaluate("navigator.userAgent"),
                "Referer": page.url,
            }
            return Resolved(file_url, name or filename_from_url(file_url), headers, await self._cookies(file_url))
        finally:
            await page.close()
