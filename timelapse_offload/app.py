"""Watch printers and render their timelapse frames when a print ends."""

import asyncio
import json
import logging
import re
import shutil
import zipfile
from datetime import datetime
from pathlib import Path

import aiohttp

from . import render
from .config import Config, Printer
from .moonraker import (SAVED_FRAMES_SUFFIX, VIDEOS_ROOT, Moonraker, MoonrakerError,
                        extract_frames)

RECONNECT_MIN, RECONNECT_MAX = 5, 300
# How long to wait after a print completes for "Save Frames" to write its zip,
# and how far apart two listings must agree on its size before we take it.
SAVED_ZIP_WAIT = 120
ZIP_STABLE_SECONDS = 5
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


def saved_video_name(zip_name: str) -> str:
    """timelapse_<file>.gcode_<date>_frames.zip -> timelapse_<file>_<date>."""
    base = zip_name[:-len(SAVED_FRAMES_SUFFIX)].replace(".gcode_", "_")
    return re.sub(r"[^A-Za-z0-9._-]+|_{2,}", "_", base).strip("_") or "timelapse"


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
        if self.printer.mode == "sync":
            await self._run_sync()
        else:
            await self._run_frames()

    async def _run_frames(self) -> None:
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

    async def _run_sync(self) -> None:
        """Poll a file root and copy new finished videos to the NAS."""
        self.log.info("sync mode: polling '%s' every %ds", self.printer.sync_root,
                      self.cfg.sync_interval)
        delay = RECONNECT_MIN
        while True:
            try:
                await self._sync_once()
                delay = RECONNECT_MIN
            except (aiohttp.ClientError, asyncio.TimeoutError, MoonrakerError, OSError) as e:
                self.log.warning("sync failed: %s (retry in %ds)", e, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, RECONNECT_MAX)
                continue
            await asyncio.sleep(self.cfg.sync_interval)

    async def _sync_once(self) -> int:
        files = await self.mr.list_root(self.printer.sync_root)
        wanted = [f for f in files
                  if any(f["path"].endswith(s) for s in self.printer.sync_suffixes)]
        dest_dir = self.cfg.output_dir / self.printer.name
        copied = 0
        for f in sorted(wanted, key=lambda x: x["path"]):
            dest = dest_dir / Path(f["path"]).name
            # Already on the NAS (same size)? Skip — makes re-polls cheap.
            try:
                if dest.exists() and dest.stat().st_size == f.get("size"):
                    continue
            except OSError:
                pass
            self.log.info("syncing %s (%.1f MB)", f["path"], f.get("size", 0) / 1e6)
            await self.mr.download_file(self.printer.sync_root, f["path"], dest)
            copied += 1
            if self.printer.delete_after_sync:
                await self.mr.delete_file(self.printer.sync_root, f["path"])
                self.log.info("deleted %s from printer", f["path"])
        if copied:
            self.log.info("synced %d new file(s) to %s", copied, dest_dir)
        return copied

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
        """Render the printer's timelapse(s). Returns the last video path.

        settle=True means a print just ended (live trigger); False is catch-up.
        """
        async with self.busy:
            try:
                if settle:
                    await asyncio.sleep(SETTLE_SECONDS)
                # Re-read so changes made in Mainsail/Fluidd since connecting apply.
                settings = await self.mr.timelapse_settings() or {}
                video = None
                if settings.get("saveframes") and not force:
                    # Only a completed print gets a saved zip; wait for it then.
                    wait = settle and self.print_state == "complete"
                    videos = await self._process_saved(settings, wait)
                    video = videos[-1] if videos else None
                # Loose frames: no zip came (cancelled print, Save Frames off), or forced.
                # Frames already rendered from their zip are marked done and skipped.
                return await self._process(settings, force) or video
            except Exception:
                self.log.exception("timelapse failed")
                return None

    async def _process_saved(self, settings: dict, wait: bool) -> list[Path]:
        """Render every saved-frames zip on the printer, deleting each once it's on the NAS."""
        videos = []
        for f in await self._saved_zips(wait):
            try:
                video = await self._render_saved(f, settings)
            except Exception:
                self.log.exception("could not render %s", f["path"])
                continue
            if video is not None:
                videos.append(video)
        return videos

    async def _saved_zips(self, wait: bool) -> list[dict]:
        """Saved-frames zips not rendered yet, once they've stopped growing.

        moonraker-timelapse writes the zip in place right after the print ends,
        so a zip only counts once its size holds across two listings. With
        wait, keep looking for up to SAVED_ZIP_WAIT for one to appear.
        """
        loop = asyncio.get_running_loop()
        start = loop.time()
        sizes: dict[str, int] = {}
        while True:
            files = [f for f in await self.mr.list_root(VIDEOS_ROOT)
                     if f["path"].endswith(SAVED_FRAMES_SUFFIX) and "/" not in f["path"]
                     and not self.state.done(self.printer.name, f["path"])]
            ready = sorted((f for f in files if sizes.get(f["path"]) == f["size"]),
                           key=lambda f: f["modified"])
            elapsed = loop.time() - start
            if files and len(ready) == len(files):
                return ready
            if not files and (not wait or elapsed >= SAVED_ZIP_WAIT):
                if wait:
                    self.log.info("no saved-frames zip appeared; using the loose frames")
                return []
            if files and elapsed >= 2 * SAVED_ZIP_WAIT:
                self.log.warning("saved-frames zip still growing; skipping it for now")
                return ready
            sizes = {f["path"]: f["size"] for f in files}
            await asyncio.sleep(ZIP_STABLE_SECONDS)

    async def _render_saved(self, f: dict, settings: dict) -> Path | None:
        name = f["path"]
        work = self.cfg.work_dir / self.printer.name / safe_name(name)
        shutil.rmtree(work, ignore_errors=True)
        try:
            self.log.info("downloading saved frames %s (%.1f MB)", name, f["size"] / 1e6)
            local = work / "frames.zip"
            await self.mr.download_file(VIDEOS_ROOT, name, local)
            try:
                got = await asyncio.to_thread(extract_frames, local, work / "frames")
            except zipfile.BadZipFile as e:
                self.log.warning("%s is not a valid zip (%s); leaving it on the printer", name, e)
                return None
            local.unlink()
            if not got:
                self.log.warning("%s has no frames", name)
                return None
            self.log.info("got %d frames from %s", got, name)
            dest = await self._render_publish(work, saved_video_name(name), settings)
            self.state.mark(self.printer.name, name)
            await self._mark_live_frames(got)
            if self.cfg.delete_saved_frames:
                await self.mr.delete_file(VIDEOS_ROOT, name)
                self.log.info("deleted %s from printer", name)
            return dest
        finally:
            shutil.rmtree(work, ignore_errors=True)

    async def _mark_live_frames(self, count: int) -> None:
        """The loose frames of the print just rendered from its zip are done too."""
        frames = await self.mr.list_frames()
        if frames and len(frames) == count:
            self.state.mark(self.printer.name, frame_set_key(frames))

    async def _render_publish(self, work: Path, name: str, settings: dict) -> Path:
        """Render work/frames to <output>/<printer>/<name>.mp4 (+ preview)."""
        async with self.render_slots:
            video, preview = await render.render(work / "frames", work / f"{name}.mp4",
                                                 settings, self.cfg.ffmpeg)
        dest_dir = self.cfg.output_dir / self.printer.name
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = publish(video, dest_dir)
        if preview is not None:
            publish(preview, dest_dir)
        self.log.info("saved %s", dest)
        return dest

    async def _process(self, settings: dict, force: bool) -> Path | None:
        frames = await self.mr.list_frames()
        if not frames:
            self.log.info("no frames on printer")
            return None
        key = frame_set_key(frames)
        if not force and self.state.done(self.printer.name, key):
            self.log.debug("frames %s already rendered", key)
            return None
        if not self.filename:
            # print_stats forgets the file after a Klippy restart; job history doesn't.
            self.filename = await self._last_job_filename()

        work = self.cfg.work_dir / self.printer.name / key
        shutil.rmtree(work, ignore_errors=True)
        try:
            self.log.info("downloading %d frames", len(frames))
            started = asyncio.get_running_loop().time()
            got = await self._fetch_frames(frames, work / "frames")
            self.log.info("downloaded %d frames in %.0fs", got,
                          asyncio.get_running_loop().time() - started)
            if not got:
                self.log.warning("no frames could be downloaded")
                return None

            stamp = datetime.now().strftime("%Y%m%d_%H%M")
            name = f"timelapse_{safe_name(self.filename)}_{stamp}"
            dest = await self._render_publish(work, name, settings)
            self.state.mark(self.printer.name, key)
            # Frames are safely rendered to the NAS now; clear them off the printer.
            if self.cfg.delete_frames_after_render:
                removed = await self.mr.delete_frames(frames, self.cfg.download_concurrency)
                self.log.info("removed %d/%d frames from printer", removed, len(frames))
            return dest
        finally:
            shutil.rmtree(work, ignore_errors=True)

    async def _fetch_frames(self, frames: list[dict], dest: Path) -> int:
        if self.printer.fetch == "zip":
            try:
                return await self.mr.download_frames_zip(frames, dest, self.printer.zip_root)
            except (aiohttp.ClientError, asyncio.TimeoutError, MoonrakerError, OSError,
                    zipfile.BadZipFile) as e:
                self.log.warning("zip download failed (%s); fetching frames one by one",
                                 e or type(e).__name__)
                shutil.rmtree(dest, ignore_errors=True)
        return await self.mr.download_frames(frames, dest, self.cfg.download_concurrency)

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
