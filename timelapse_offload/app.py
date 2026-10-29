"""Watch printers and render their timelapse frames when a print ends."""

import asyncio
import json
import logging
import re
import shutil
from datetime import datetime
from pathlib import Path

import aiohttp

from . import render
from .config import Config, Printer
from .moonraker import Moonraker, MoonrakerError

RECONNECT_MIN, RECONNECT_MAX = 5, 300
NO_TIMELAPSE_RETRY = 600
# moonraker-timelapse may still be saving the last frame when print_stats flips to complete.
SETTLE_SECONDS = 5
KEEP_KEYS = 50
ACTIVE_STATES = {"printing", "paused"}


class State:
    """Which frame sets have been rendered, per printer, so restarts don't repeat work."""

    def __init__(self, path: Path):
        self.path = path
        try:
            self.data: dict[str, list[str]] = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            self.data = {}

    def done(self, printer: str, key: str) -> bool:
        return key in self.data.get(printer, [])

    def mark(self, printer: str, key: str) -> None:
        keys = self.data.setdefault(printer, [])
        keys.append(key)
        del keys[:-KEEP_KEYS]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        tmp.replace(self.path)


def frame_set_key(frames: list[dict]) -> str:
    return f"{frames[0]['modified']:.0f}-{len(frames)}"


def safe_name(gcode_path: str) -> str:
    stem = Path(gcode_path or "unknown").stem
    return re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("_") or "unknown"


class Watcher:
    def __init__(self, printer: Printer, cfg: Config, state: State,
                 session: aiohttp.ClientSession, render_slots: asyncio.Semaphore):
        self.printer = printer
        self.cfg = cfg
        self.state = state
        self.render_slots = render_slots
        self.mr = Moonraker(printer, session)
        self.log = self.mr.log
        self.print_state = ""
        self.filename = ""
        self.busy = asyncio.Lock()
        self.tasks: set[asyncio.Task] = set()

    async def run(self) -> None:
        delay = RECONNECT_MIN
        while True:
            try:
                delay = await self._session() or RECONNECT_MIN
            except (aiohttp.ClientError, asyncio.TimeoutError, MoonrakerError, OSError) as e:
                self.log.warning("connection lost: %s (retry in %ds)", e or type(e).__name__, delay)
            finally:
                await self.mr.close()
            await asyncio.sleep(delay)
            delay = min(delay * 2, RECONNECT_MAX)

    async def _session(self) -> int | None:
        """One websocket session. Returns a retry delay to use instead of the backoff."""
        settings = await self.mr.timelapse_settings()
        if settings is None:
            self.log.warning("moonraker-timelapse not installed; checking again in %ds",
                             NO_TIMELAPSE_RETRY)
            return NO_TIMELAPSE_RETRY
        if self.cfg.disable_printer_render and settings.get("autorender"):
            await self.mr.post("/machine/timelapse/settings", autorender="false")
            self.log.info("turned off on-printer rendering (autorender)")

        await self.mr.connect()
        notifications: asyncio.Queue = asyncio.Queue()

        async def pump() -> None:
            async for note in self.mr.messages():
                notifications.put_nowait(note)
            notifications.put_nowait(None)

        reader = asyncio.create_task(pump())
        try:
            await self._subscribe()
            self.log.info("connected, print state %s", self.print_state or "unknown")
            if self.cfg.catch_up and self.print_state not in ACTIVE_STATES:
                if self.print_state not in ("cancelled", "error") or self.cfg.render_cancelled:
                    self._spawn(self.process(settle=False))
            while (note := await notifications.get()) is not None:
                await self._handle(note)
        finally:
            reader.cancel()
        return None

    async def _subscribe(self) -> None:
        try:
            result = await self.mr.call("printer.objects.subscribe",
                                        objects={"print_stats": ["state", "filename"]})
        except MoonrakerError as e:
            # Klippy not ready yet; notify_klippy_ready will trigger a resubscribe.
            self.log.info("subscribe failed (%s), waiting for Klippy", e)
            return
        self._update(result["status"].get("print_stats", {}))

    def _update(self, stats: dict) -> str | None:
        """Apply a print_stats update; return the previous state if it changed."""
        if "filename" in stats and stats["filename"]:
            self.filename = stats["filename"]
        new = stats.get("state")
        if new is None or new == self.print_state:
            return None
        old, self.print_state = self.print_state, new
        return old

    async def _handle(self, note: dict) -> None:
        method = note["method"]
        if method == "notify_klippy_ready":
            await self._subscribe()
        elif method == "notify_status_update":
            old = self._update(note["params"][0].get("print_stats", {}))
            if old is None:
                return
            self.log.info("print state %s -> %s", old or "unknown", self.print_state)
            if old not in ACTIVE_STATES:
                return
            if self.print_state == "complete" or (
                    self.print_state in ("cancelled", "error") and self.cfg.render_cancelled):
                self._spawn(self.process())

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def process(self, settle: bool = True, force: bool = False) -> Path | None:
        """Fetch the printer's current frames and render them. Returns the video path."""
        async with self.busy:
            try:
                if settle:
                    await asyncio.sleep(SETTLE_SECONDS)
                return await self._process(force)
            except Exception:
                self.log.exception("timelapse failed")
                return None

    async def _process(self, force: bool) -> Path | None:
        frames = await self.mr.list_frames()
        if not frames:
            self.log.info("no frames on printer")
            return None
        key = frame_set_key(frames)
        if not force and self.state.done(self.printer.name, key):
            self.log.debug("frames %s already rendered", key)
            return None
        # Re-read so changes made in Mainsail/Fluidd since connecting apply.
        settings = await self.mr.timelapse_settings() or {}
        if not self.filename:
            # print_stats forgets the file after a Klippy restart; job history doesn't.
            self.filename = await self._last_job_filename()

        work = self.cfg.work_dir / self.printer.name / key
        shutil.rmtree(work, ignore_errors=True)
        try:
            self.log.info("downloading %d frames", len(frames))
            started = asyncio.get_running_loop().time()
            await self.mr.download_frames(frames, work / "frames", self.cfg.download_concurrency)
            self.log.info("downloaded in %.0fs", asyncio.get_running_loop().time() - started)

            stamp = datetime.now().strftime("%Y%m%d_%H%M")
            name = f"timelapse_{safe_name(self.filename)}_{stamp}"
            async with self.render_slots:
                video, preview = await render.render(work / "frames", work / f"{name}.mp4",
                                                     settings, self.cfg.ffmpeg)
            dest_dir = self.cfg.output_dir / self.printer.name
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = publish(video, dest_dir)
            if preview is not None:
                publish(preview, dest_dir)
            self.state.mark(self.printer.name, key)
            self.log.info("saved %s", dest)
            return dest
        finally:
            shutil.rmtree(work, ignore_errors=True)

    async def _last_job_filename(self) -> str:
        try:
            jobs = (await self.mr.get("/server/history/list", limit=1, order="desc"))["jobs"]
        except (MoonrakerError, KeyError):
            return ""
        return jobs[0].get("filename", "") if jobs else ""


def publish(src: Path, dest_dir: Path) -> Path:
    """Copy to the (network) output folder under a temp name, then rename into place."""
    dest = dest_dir / src.name
    part = dest_dir / (src.name + ".part")
    shutil.copyfile(src, part)
    part.replace(dest)
    return dest


async def serve(cfg: Config) -> None:
    state = State(cfg.state_file)
    slots = asyncio.Semaphore(cfg.max_parallel_renders)
    async with aiohttp.ClientSession() as session:
        watchers = [Watcher(p, cfg, state, session, slots) for p in cfg.printers]
        await asyncio.gather(*(w.run() for w in watchers))


async def render_now(cfg: Config, name: str) -> Path | None:
    """Render whatever frames are on one printer right now, even if done before."""
    printer = next((p for p in cfg.printers if p.name.lower() == name.lower()), None)
    if printer is None:
        raise SystemExit(f"no printer named {name!r} in config")
    async with aiohttp.ClientSession() as session:
        w = Watcher(printer, cfg, State(cfg.state_file), session, asyncio.Semaphore(1))
        stats = await w.mr.get("/printer/objects/query", print_stats="state,filename")
        w._update(stats["status"].get("print_stats", {}))
        return await w.process(settle=False, force=True)
