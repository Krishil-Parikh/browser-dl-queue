"""browser-dl-queue: the TUI. Run with:  python app.py

Flow:  Fetch links -> review the list (delete what you don't want) -> Continue.
Everything works with the mouse: click a row's [ ] to tick it, its pause/play
icon to pause/resume, its x to delete, or use the toolbar for ticked rows.
"""
import os
import subprocess
import sys
from pathlib import Path

from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Input, RichLog, Static, TextArea

from browser import Browser, HumanCheckNeeded
from config import Config
from jobs import ACTIVE, DONE, DOWNLOADING, FAILED, PAUSED, PENDING, RESOLVING, Manager

STATUS_STYLE = {
    PENDING: "dim", RESOLVING: "yellow", DOWNLOADING: "cyan",
    DONE: "green", FAILED: "bold red", PAUSED: "magenta",
}
COLUMNS = [("", "check"), ("#", "idx"), ("File", "name"), ("Status", "status"),
           ("Progress", "bar"), ("Size", "size"), ("Speed", "speed"), ("ETA", "eta"),
           ("Try", "tries"), ("", "toggle"), ("", "delete")]
CLICKABLE = {"check", "toggle", "delete"}


def human(n) -> str:
    if not n:
        return "-"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def clock(sec) -> str:
    sec = int(sec)
    h, m, s = sec // 3600, sec // 60 % 60, sec % 60
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


def bar(job, width=20) -> Text:
    if job.status == DONE:
        frac = 1.0
    elif job.total:
        frac = job.done / job.total
    else:
        return Text("·" * width + "    ?", style="dim") if job.status == DOWNLOADING else Text("")
    filled = int(frac * width)
    style = STATUS_STYLE.get(job.status, "white")
    return Text.assemble(("█" * filled, style), ("░" * (width - filled), "grey35"), f" {frac:4.0%}")


def toggle_icon(job) -> Text:
    if job.status in ACTIVE or job.status == PENDING:
        return Text(" ⏸ ", style="bold magenta")
    if job.status == PAUSED:
        return Text(" ▶ ", style="bold green")
    if job.status == FAILED:
        return Text(" ↻ ", style="bold yellow")
    return Text("")


class JobTable(DataTable):
    """DataTable that also reports which *column* was clicked, so cells can act as buttons."""

    class CellClicked(Message):
        def __init__(self, row_key: str, column_key: str):
            super().__init__()
            self.row_key, self.column_key = row_key, column_key

    def on_click(self, event: events.Click) -> None:
        meta = event.style.meta
        row, col = meta.get("row", -1), meta.get("column", -1)
        if row is None or col is None or row < 0 or col < 0 or row >= self.row_count:
            return
        row_key = self.ordered_rows[row].key.value
        col_key = self.ordered_columns[col].key.value
        if col_key in CLICKABLE:
            self.post_message(self.CellClicked(row_key, col_key))


class AddLinks(ModalScreen):
    """Paste one or many URLs (one per line)."""

    BINDINGS = [Binding("escape", "dismiss_none", "Cancel")]

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static("[b]Add links[/b]  one URL per line")
            yield TextArea(id="urls")
            with Horizontal(id="dialog-buttons"):
                yield Button("Add", variant="primary", id="ok")
                yield Button("Cancel", id="cancel")

    def on_button_pressed(self, event: Button.Pressed):
        self.dismiss(self.query_one("#urls", TextArea).text if event.button.id == "ok" else None)

    def action_dismiss_none(self):
        self.dismiss(None)


class DLQueue(App):
    TITLE = "browser-dl-queue"
    CSS = """
    #settings { height: auto; padding: 0 1; }
    #settings Horizontal { height: 3; }
    #settings Input { width: 1fr; }
    #settings Button { min-width: 14; margin-left: 1; }
    #toolbar { height: 1; padding: 0 1; margin: 1 0 0 0; }
    #toolbar Button { margin-right: 1; }
    #stats { height: 1; padding: 0 1; background: $boost; margin-top: 1; }
    #hint { height: 1; padding: 0 1; color: $warning; }
    #hint.alert { background: $error; color: $text; text-style: bold; }
    #detail { height: 1; padding: 0 1; color: $text-muted; }
    JobTable { height: 1fr; }
    RichLog { height: 8; border-top: solid $primary; }
    AddLinks { align: center middle; }
    #dialog { width: 90; height: 22; border: thick $primary; background: $surface; padding: 1; }
    #dialog TextArea { height: 1fr; }
    #dialog-buttons { height: 3; align-horizontal: right; }
    """
    BINDINGS = [
        Binding("f", "fetch", "Fetch"),
        Binding("s", "toggle_run", "Continue/Stop"),
        Binding("space", "check", "Tick"),
        Binding("delete,x", "delete", "Delete"),
        Binding("p", "toggle_job", "Pause/Resume"),
        Binding("a", "add", "Add URLs"),
        Binding("ctrl+a", "check_all", "Tick all", show=False),
        Binding("left_square_bracket", "move(-1)", "Up", show=False),
        Binding("right_square_bracket", "move(1)", "Down", show=False),
        Binding("plus,equals_sign", "slots(1)", "+Slot", show=False),
        Binding("minus", "slots(-1)", "-Slot", show=False),
        Binding("h", "toggle_headless", "Headless", show=False),
        Binding("escape", "focus_table", "Table", show=False),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self):
        super().__init__()
        self.cfg = Config.load()
        self.browser = Browser(self.cfg, log=self.log_msg)
        self.manager = Manager(self.cfg, self.browser, log=self.log_msg)
        self.manager.load()
        self.checked: set[int] = set()
        self._row_ids: list[int] = []
        self._was_verifying = False

    # ---------- layout ----------
    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="settings"):
            with Horizontal():
                yield self._input("source_url", "Source page URL")
                yield self._input("link_pattern", "Link filter (regex)")
                yield Button("① Fetch links", variant="primary", id="fetch")
            with Horizontal():
                yield self._input("download_selector", "Download button selector (empty = direct links)")
                yield self._input("download_dir", "Download folder")
                yield Button("Save settings", id="save")
        with Horizontal(id="toolbar"):
            yield Button("② ▶ Continue", variant="success", id="run", compact=True)
            yield Button("☑ All", id="check_all", compact=True)
            yield Button("☐ None", id="check_none", compact=True)
            yield Button("✕ Delete", variant="error", id="delete", compact=True)
            yield Button("⏸ Pause", id="pause", compact=True)
            yield Button("▶ Resume", id="resume", compact=True)
            yield Button("↻ Retry failed", id="retry_all", compact=True)
            yield Button("↑", id="up", compact=True)
            yield Button("↓", id="down", compact=True)
            yield Button("− slot", id="slot_minus", compact=True)
            yield Button("+ slot", id="slot_plus", compact=True)
            yield Button("+ Add URLs", id="add", compact=True)
            yield Button("Clear done", id="clear_done", compact=True)
            yield Button("📂 Folder", id="folder", compact=True)
        yield Static(id="stats")
        yield Static(id="hint")
        table = JobTable(id="jobs", cursor_type="row", zebra_stripes=True)
        for label, key in COLUMNS:
            table.add_column(label, key=key)
        yield table
        yield Static(id="detail")
        yield RichLog(id="log", markup=True, wrap=True)
        yield Footer()

    def _input(self, key, title) -> Input:
        inp = Input(value=str(getattr(self.cfg, key)), placeholder=title, id=key)
        inp.border_title = title
        return inp

    def on_mount(self):
        self.table = self.query_one("#jobs", JobTable)
        self.run_worker(self.manager.run(), group="scheduler")
        self.set_interval(0.5, self.refresh_view)
        self.refresh_view()
        self.table.focus()
        self.log_msg(f"{len(self.manager.jobs)} jobs loaded from last session.")

    # ---------- helpers ----------
    def log_msg(self, msg: str):
        try:
            self.query_one("#log", RichLog).write(msg)
        except Exception:
            print(msg)

    def apply_settings(self):
        for key in ("source_url", "link_pattern", "download_selector", "download_dir"):
            setattr(self.cfg, key, self.query_one(f"#{key}", Input).value.strip())
        self.cfg.save()

    def cursor_job(self):
        if not self.table.row_count:
            return None
        row_key, _ = self.table.coordinate_to_cell_key(self.table.cursor_coordinate)
        return self.manager.get(int(row_key.value))

    def targets(self):
        """Ticked jobs, or the highlighted one if nothing is ticked."""
        if self.checked:
            return [j for j in self.manager.jobs if j.id in self.checked]
        job = self.cursor_job()
        return [job] if job else []

    def cells(self, i, job):
        speed = human(job.speed) + "/s" if job.status == DOWNLOADING and job.speed else ""
        eta = clock((job.total - job.done) / job.speed) if job.speed and job.total else ""
        size = f"{human(job.done)} / {human(job.total)}" if job.total else human(job.done)
        name = job.name or job.url
        check = Text(" ☑ ", style="bold green") if job.id in self.checked else Text(" ☐ ", style="dim")
        return [check, str(i), name if len(name) <= 50 else "…" + name[-49:],
                Text(job.status, style=STATUS_STYLE[job.status]), bar(job),
                size, speed, eta, str(job.attempts or ""), toggle_icon(job), Text(" ✕ ", style="bold red")]

    def refresh_view(self):
        m, t = self.manager, self.table
        self.checked &= {j.id for j in m.jobs}
        ids = [j.id for j in m.jobs]
        if ids != self._row_ids:  # rows added/removed/moved: rebuild, keep cursor
            row = t.cursor_row
            t.clear()
            for i, job in enumerate(m.jobs, 1):
                t.add_row(*self.cells(i, job), key=str(job.id))
            self._row_ids = ids
            if t.row_count:
                t.move_cursor(row=min(row, t.row_count - 1))
        else:
            for i, job in enumerate(m.jobs, 1):
                for (_, col), value in zip(COLUMNS, self.cells(i, job)):
                    t.update_cell(str(job.id), col, value)

        count = lambda s: sum(j.status == s for j in m.jobs)
        total_speed = sum(j.speed for j in m.jobs if j.status == DOWNLOADING)
        state = "[b green]▶ RUNNING[/]" if m.running else "[b yellow]⏸ STOPPED[/]"
        wait = m.seconds_to_next_start()
        if m.running and wait and count(PENDING) and len(m.tasks) < self.cfg.max_concurrent:
            state += f" [dim](next start in {wait:.0f}s)[/]"
        mode = f"click [b]{self.cfg.download_selector}[/]" if self.cfg.download_selector else "direct"
        self.query_one("#stats", Static).update(
            f"{state}   slots [b]{len(m.tasks)}/{self.cfg.max_concurrent}[/]   "
            f"total {len(m.jobs)}  pending {count(PENDING)}  paused {count(PAUSED)}  "
            f"done [green]{count(DONE)}[/]  failed [red]{count(FAILED)}[/]  ticked {len(self.checked)}   "
            f"⬇ [b]{human(total_speed)}/s[/]   gap {m.gap_range()[0]:.0f}-{m.gap_range()[1]:.0f}s"
            f"{f' [yellow](slowed x{m.slowdown:.1f})[/]' if m.slowdown > 1 else ''}   mode: {mode}   "
            f"browser: {'headless' if self.cfg.headless else 'visible'}"
        )
        run_btn = self.query_one("#run", Button)
        run_btn.label = "⏸ Stop" if m.running else "② ▶ Continue"
        run_btn.variant = "warning" if m.running else "success"

        verifying = self.browser.verifying
        if verifying and not self._was_verifying:
            self.bell()
            self.notify("Human check: complete it in the browser window", severity="error", timeout=10)
        self._was_verifying = verifying
        hint_widget = self.query_one("#hint", Static)
        hint_widget.set_class(verifying, "alert")

        if verifying:
            hint = (f"🛑 HUMAN CHECK - complete it in the browser window. Everything is on hold "
                    f"until the page is ready.  ({self.browser.verifying_url})")
        elif not m.jobs:
            hint ="Step 1: enter the source page URL + filter, then click ① Fetch links (or + Add URLs)."
        elif not m.running and count(PENDING):
            hint = "Step 2: review the list - click ✕ to drop a file, ☐ to tick several - then click ② ▶ Continue."
        else:
            hint = ""
        hint_widget.update(Text(hint))

        job = self.cursor_job()
        detail = Text()
        if job:
            detail.append(job.url)
            if job.error:
                detail.append("   " + job.error, style="red")
        self.query_one("#detail", Static).update(detail)

    # ---------- mouse ----------
    def on_job_table_cell_clicked(self, msg: JobTable.CellClicked):
        job = self.manager.get(int(msg.row_key))
        if not job:
            return
        if msg.column_key == "check":
            self.checked ^= {job.id}
        elif msg.column_key == "toggle":
            self._toggle(job)
        elif msg.column_key == "delete":
            self._delete([job])
        self.refresh_view()

    def on_button_pressed(self, event: Button.Pressed):
        actions = {
            "fetch": self.action_fetch, "save": self.action_save, "run": self.action_toggle_run,
            "check_all": self.action_check_all, "check_none": self.action_check_none,
            "delete": self.action_delete, "pause": self.action_pause, "resume": self.action_resume,
            "retry_all": self.action_retry_all, "up": lambda: self.action_move(-1),
            "down": lambda: self.action_move(1), "slot_minus": lambda: self.action_slots(-1),
            "slot_plus": lambda: self.action_slots(1), "add": self.action_add,
            "clear_done": self.action_clear_done, "folder": self.action_open_folder,
        }
        if event.button.id in actions:
            actions[event.button.id]()
            self.refresh_view()

    def on_input_submitted(self, event: Input.Submitted):
        self.action_save()
        self.table.focus()

    # ---------- actions ----------
    def _toggle(self, job):
        if job.status in ACTIVE or job.status == PENDING:
            self.manager.pause(job)
        else:
            self.manager.resume(job)  # paused or failed -> pending
        self.manager.save()

    def _delete(self, jobs):
        for job in jobs:
            self.manager.remove(job)
            self.checked.discard(job.id)
        self.manager.save()
        if jobs:
            self.log_msg(f"removed {len(jobs)} item(s)")

    def action_save(self):
        self.apply_settings()
        self.notify("Settings saved")

    def action_focus_table(self):
        self.table.focus()

    def action_toggle_run(self):
        self.apply_settings()
        self.manager.running = not self.manager.running
        if self.manager.running:
            self.log_msg(f"[green]continuing[/]: up to {self.cfg.max_concurrent} downloads at once")
        else:
            self.log_msg("[yellow]stopped[/]: no new downloads start; active ones keep going (⏸ Pause to stop them)")

    def action_fetch(self):
        self.apply_settings()
        if not self.cfg.source_url:
            self.notify("Enter a source page URL first", severity="warning")
            return
        self.manager.running = False  # always review before downloading
        self.run_worker(self._fetch(self.cfg.source_url), group="fetch", exclusive=True)

    async def _fetch(self, url):
        self.log_msg(f"reading links from {url} ...")
        try:
            links = await self.browser.extract_links(url)
        except HumanCheckNeeded:
            self.log_msg("[red]fetch stopped:[/] the page shows a human check but the browser is headless. "
                         "Press [b]h[/] for a visible browser and fetch again.")
            return
        except Exception as e:
            self.log_msg(f"[red]fetch failed:[/] {e}")
            return
        added = sum(self.manager.add(link["url"]) for link in links)
        self.manager.save()
        self.log_msg(f"found {len(links)} matching links, [b]{added}[/] new. Review, then ② ▶ Continue.")

    def action_add(self):
        def done(text):
            if not text:
                return
            urls = [u for u in text.split() if u.startswith("http")]
            added = sum(self.manager.add(u) for u in urls)
            self.manager.save()
            self.log_msg(f"added {added} of {len(urls)} URLs")
        self.push_screen(AddLinks(), done)

    def action_check(self):
        job = self.cursor_job()
        if job:
            self.checked ^= {job.id}
            if self.table.cursor_row < self.table.row_count - 1:
                self.table.move_cursor(row=self.table.cursor_row + 1)

    def action_check_all(self):
        self.checked = {j.id for j in self.manager.jobs}

    def action_check_none(self):
        self.checked.clear()

    def action_delete(self):
        self._delete(self.targets())

    def action_toggle_job(self):
        for job in self.targets():
            self._toggle(job)

    def action_pause(self):
        for job in self.targets():
            if job.status in ACTIVE or job.status == PENDING:
                self.manager.pause(job)
        self.manager.save()

    def action_resume(self):
        for job in self.targets():
            self.manager.resume(job)
        self.manager.save()

    def action_retry_all(self):
        for job in self.manager.jobs:
            if job.status == FAILED:
                self.manager.resume(job)

    def action_move(self, delta: int):
        job = self.cursor_job()
        if job:
            self.manager.move(job, delta)
            self.refresh_view()
            self.table.move_cursor(row=self.manager.jobs.index(job))
            self.manager.save()

    def action_slots(self, delta: int):
        self.cfg.max_concurrent = max(1, min(16, self.cfg.max_concurrent + delta))
        self.cfg.save()

    def action_clear_done(self):
        self.manager.clear_done()
        self.manager.save()

    async def action_toggle_headless(self):
        if self.manager.tasks:
            self.notify("Pause active jobs first", severity="warning")
            return
        self.cfg.headless = not self.cfg.headless
        self.cfg.save()
        await self.browser.stop()  # relaunches in the new mode on next use

    def action_open_folder(self):
        path = Path(self.cfg.download_dir).resolve()
        path.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(path)
        else:
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)])

    async def action_quit(self):
        self.log_msg("saving and closing ...")
        await self.manager.shutdown()
        await self.browser.stop()
        self.exit()


if __name__ == "__main__":
    DLQueue().run()
