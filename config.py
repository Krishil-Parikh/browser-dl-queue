"""Settings, stored in config.json next to the app."""
import json
from dataclasses import asdict, dataclass, fields
from pathlib import Path

CONFIG_PATH = Path("config.json")


@dataclass
class Config:
    source_url: str = ""            # page that lists the links
    link_pattern: str = r".*"       # regex: only links matching this are queued
    wait_for_selector: str = "body" # wait for this before reading links (JS-rendered pages)
    render_wait_ms: int = 2000      # extra settle time after the selector appears
    download_selector: str = ""     # button to click on each link's page; empty = links are direct files
    download_dir: str = "downloads"
    max_concurrent: int = 3
    start_gap_min_s: float = 10     # wait a random gap between starting one job and the next
    start_gap_max_s: float = 20
    headless: bool = False          # False = you can watch the browser work
    close_popups: bool = True       # auto-close popups/new tabs opened by clicks
    click_attempts: int = 3         # re-click if the first click doesn't start a download
    verify_timeout_s: int = 600     # how long to wait for you to finish a "verify you are human" page
    slowdown_factor: float = 1.5    # each human check multiplies the start gap by this...
    max_slowdown: float = 4.0       # ...up to this many times the original gap
    retries: int = 3                # attempts per file before marking it failed
    timeout_s: int = 60

    @classmethod
    def load(cls, path: Path = CONFIG_PATH) -> "Config":
        if not path.exists():
            cfg = cls()
            cfg.save(path)
            return cfg
        data = json.loads(path.read_text(encoding="utf-8"))
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    def save(self, path: Path = CONFIG_PATH) -> None:
        path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
