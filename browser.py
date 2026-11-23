"""Everything that touches Playwright lives here.

Two jobs:
  1. extract_links(url)  -> read every link from a (possibly JS-rendered) page
  2. resolve(url)        -> open a link's page, click the download button, and
                            capture the real file URL + cookies so the actual
                            transfer can run over HTTP with live progress.
"""
import asyncio
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from urllib.parse import unquote, urlparse

from playwright.async_api import TimeoutError as PWTimeout
from playwright.async_api import async_playwright

URL_IN_TEXT = re.compile(r"https?://[^\s<>\"']+")


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
        self._pw = None
        self._browser = None
        self.context = None
        self._start_lock = asyncio.Lock()

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
        await self.start()
        page = await self.context.new_page()
        if self.cfg.close_popups:
            page.on("popup", lambda p: asyncio.ensure_future(p.close()))
        return page

    async def _cookies(self, url):
        return {c["name"]: c["value"] for c in await self.context.cookies(url)}

    async def extract_links(self, url: str) -> list[dict]:
        """Return [{text, url}] for links matching cfg.link_pattern, de-duplicated, in page order."""
        page = await self._new_page()
        timeout = self.cfg.timeout_s * 1000
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            if self.cfg.wait_for_selector:
                await page.wait_for_selector(self.cfg.wait_for_selector, timeout=timeout)
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
            return Resolved(url, filename_from_url(url), cookies=await self._cookies(url))

        page = await self._new_page()
        timeout = self.cfg.timeout_s * 1000
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            await page.wait_for_selector(selector, timeout=timeout)
            download = None
            for attempt in range(1, self.cfg.click_attempts + 1):
                try:
                    async with page.expect_download(timeout=15_000) as info:
                        await page.click(selector, timeout=timeout)
                    download = await info.value
                    break
                except PWTimeout:
                    self.log(f"click {attempt} on {selector!r} started no download, retrying")
            if download is None:
                raise RuntimeError(f"clicking {selector!r} never started a download")

            # hand the transfer over to our own HTTP client (progress + resume)
            file_url, name = download.url, download.suggested_filename
            await download.cancel()
            headers = {
                "User-Agent": await page.evaluate("navigator.userAgent"),
                "Referer": page.url,
            }
            return Resolved(file_url, name or filename_from_url(file_url), headers, await self._cookies(file_url))
        finally:
            await page.close()
