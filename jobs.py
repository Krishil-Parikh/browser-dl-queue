"""The queue: jobs, their states, and the scheduler that keeps N slots busy."""
import asyncio
import itertools
import json
import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from browser import HumanCheckNeeded
from downloader import fetch

STATE_PATH = Path("state.json")

PENDING, RESOLVING, DOWNLOADING = "pending", "resolving", "downloading"
DONE, FAILED, PAUSED = "done", "failed", "paused"
ACTIVE = (RESOLVING, DOWNLOADING)

_ids = itertools.count(1)


@dataclass
class Job:
    url: str
    name: str = ""
    status: str = PENDING
    done: int = 0
    total: int | None = None
    speed: float = 0.0  # bytes/s
    error: str = ""
    attempts: int = 0
    id: int = field(default_factory=lambda: next(_ids))

    def to_dict(self):
        status = PENDING if self.status in ACTIVE else self.status
        return {"url": self.url, "name": self.name, "status": status,
                "done": self.done, "total": self.total}


def safe_name(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip() or "download"


class Manager:
    def __init__(self, cfg, browser, log=print):
        self.cfg = cfg
        self.browser = browser
        self.log = log
        self.jobs: list[Job] = []
        self.tasks: dict[int, asyncio.Task] = {}
        self.running = False
        self.shutting_down = False
        self.next_start_at = 0.0  # monotonic time before which no new job may start
        self.slowdown = 1.0       # grows each time a human check shows up
        browser.on_challenge = self._on_challenge

    def _on_challenge(self, url):
        self.slowdown = min(self.cfg.max_slowdown, self.slowdown * self.cfg.slowdown_factor)
        lo, hi = self.gap_range()
        self.log(f"[yellow]slowing down[/]: start gap is now {lo:.0f}-{hi:.0f}s (x{self.slowdown:.1f})")

    def gap_range(self):
        lo = max(0.0, self.cfg.start_gap_min_s) * self.slowdown
        hi = max(lo, self.cfg.start_gap_max_s * self.slowdown)
        return lo, hi

    # ---------- persistence ----------
    def load(self):
        if STATE_PATH.exists():
            for d in json.loads(STATE_PATH.read_text(encoding="utf-8")):
                self.jobs.append(Job(**d))

    def save(self):
        STATE_PATH.write_text(json.dumps([j.to_dict() for j in self.jobs], indent=1), encoding="utf-8")

    # ---------- queue editing ----------
    def get(self, job_id) -> Job | None:
        return next((j for j in self.jobs if j.id == job_id), None)

    def add(self, url: str, name: str = "") -> bool:
        if any(j.url == url for j in self.jobs):
            return False
        self.jobs.append(Job(url=url, name=name))
        return True

    def pause(self, job: Job):
        if job.id in self.tasks:
            self.tasks[job.id].cancel()  # _work marks it paused
        elif job.status == PENDING:
            job.status = PAUSED

    def resume(self, job: Job):
        if job.status in (PAUSED, FAILED):
            job.status, job.error, job.attempts = PENDING, "", 0

    def remove(self, job: Job):
        if job.id in self.tasks:
            self.tasks[job.id].cancel()
        self.jobs.remove(job)

    def move(self, job: Job, delta: int):
        i = self.jobs.index(job)
        j = max(0, min(len(self.jobs) - 1, i + delta))
        self.jobs.insert(j, self.jobs.pop(i))

    def clear_done(self):
        self.jobs = [j for j in self.jobs if j.status != DONE]

    def pause_all(self):
        self.running = False
        for t in list(self.tasks.values()):
            t.cancel()

    async def shutdown(self):
        self.shutting_down = True
        self.running = False
        tasks = list(self.tasks.values())
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.save()

    # ---------- scheduling ----------
    def seconds_to_next_start(self) -> float:
        return max(0.0, self.next_start_at - time.monotonic())

    async def run(self):
        """Completion-based: whenever a slot is free and work is pending, start it,
        but never start two jobs closer together than a random start gap."""
        while True:
            if self.browser.verifying:
                # nothing new starts while you verify, and the full gap applies after you finish
                self.next_start_at = max(self.next_start_at, time.monotonic() + self.gap_range()[0])
            elif self.running and len(self.tasks) < self.cfg.max_concurrent and not self.seconds_to_next_start():
                job = next((j for j in self.jobs if j.status == PENDING and j.id not in self.tasks), None)
                if job is not None:
                    job.status = RESOLVING
                    self.tasks[job.id] = asyncio.create_task(self._work(job))
                    self.next_start_at = time.monotonic() + random.uniform(*self.gap_range())
            await asyncio.sleep(0.3)

    async def _work(self, job: Job):
        try:
            while True:
                job.attempts += 1
                job.status, job.error = RESOLVING, ""
                try:
                    resolved = await self.browser.resolve(job.url)
                    job.name = job.name or safe_name(resolved.filename)
                    job.status = DOWNLOADING
                    self.log(f"[cyan]start[/] {job.name}")
                    await fetch(resolved, Path(self.cfg.download_dir) / job.name,
                                self._progress(job), self.cfg.timeout_s)
                    job.status, job.speed = DONE, 0.0
                    self.log(f"[green]done[/]  {job.name}")
                    return
                except asyncio.CancelledError:
                    raise
                except HumanCheckNeeded:
                    self.running = False
                    job.status, job.attempts = PENDING, 0
                    job.error = "human check appeared - press h for a visible browser, then Continue"
                    self.log(f"[b red]queue stopped[/]: human check on {job.url}, but the browser is headless. "
                             "Press [b]h[/] for a visible browser, then Continue.")
                    return
                except Exception as e:
                    job.error = f"{type(e).__name__}: {e}"[:300]
                    job.speed = 0.0
                    if job.attempts >= self.cfg.retries:
                        job.status = FAILED
                        self.log(f"[red]fail[/]  {job.name or job.url} - {job.error}")
                        return
                    wait = min(60, 2 ** job.attempts)
                    self.log(f"[yellow]retry[/] {job.name or job.url} in {wait}s - {job.error}")
                    await asyncio.sleep(wait)
        except asyncio.CancelledError:
            job.speed = 0.0
            if job.status != DONE:
                job.status = PENDING if self.shutting_down else PAUSED
        finally:
            self.tasks.pop(job.id, None)
            if not self.shutting_down:
                self.save()

    @staticmethod
    def _progress(job: Job):
        last = {"t": time.monotonic(), "b": None}

        def cb(done, total):
            job.done, job.total = done, total
            now = time.monotonic()
            if last["b"] is None:
                last["t"], last["b"] = now, done
            elif now - last["t"] >= 0.5:
                inst = (done - last["b"]) / (now - last["t"])
                job.speed = inst if job.speed == 0 else 0.7 * job.speed + 0.3 * inst
                last["t"], last["b"] = now, done
        return cb
