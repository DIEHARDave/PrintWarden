"""Watch printing printers through their webcam and act on likely failures."""

import asyncio
import json
import logging
import time
from datetime import datetime
from collections import deque
from pathlib import Path

import aiohttp

from . import detect, faults, motion, training
from .config import Config, Printer
from .detect import ESCALATING_FACTOR, Model, Prediction, PredictionStore, Snapshot
from .moonraker import Moonraker, MoonrakerError

SNAPSHOT_TIMEOUT = aiohttp.ClientTimeout(total=15)
WEBCAM_REFRESH = 600
HISTORY = 360          # score points kept for the page (1 h at 10 s)
IDLE_POLL = 15         # print-state poll while not printing
ACTIVE = ("printing", "paused")


def snapshot_url(printer: Printer, cam: dict) -> str:
    url = cam.get("snapshot_url") or ""
    if url.startswith(("http://", "https://")):
        return url
    if not url.startswith("/"):
        url = "/" + url
    return f"http://{printer.host}{url}"


def stream_url(printer: Printer, cam: dict | None) -> str:
    """MJPEG stream the browser can show in an <img>, or "" if the camera has none.
    camera-streamer lists its WebRTC page; the same server also serves stream.mjpg."""
    if not cam:
        return ""
    url = cam.get("stream_url") or ""
    service = cam.get("service") or ""
    if service.startswith("webrtc-camerastreamer") and url.rstrip("/").endswith("/webrtc"):
        url = url.rstrip("/")[:-len("webrtc")] + "stream.mjpg"
    elif service not in ("mjpegstreamer", "mjpegstreamer-adaptive", "uv4l-mjpeg") or not url:
        return ""
    return snapshot_url(printer, {"snapshot_url": url})


def pick_webcam(cams: list[dict], wanted: str) -> dict | None:
    usable = [c for c in cams if c.get("enabled", True) and c.get("snapshot_url")
              and c.get("service") != "iframe"]
    if wanted:
        return next((c for c in usable if c.get("name") == wanted), None)
    return usable[0] if usable else None


class Notifier:
    def __init__(self, cfg: Config, session: aiohttp.ClientSession):
        self.cfg = cfg
        self.session = session
        self.log = logging.getLogger("notify")

    async def send(self, mr: Moonraker, title: str, text: str, color: int,
                   snap: Snapshot | None, before: bytes | None = None) -> None:
        if self.cfg.notify.console:
            msg = f"{title}: {text}".replace('"', "'").replace("\n", " ")
            try:
                await mr.post("/printer/gcode/script", script=f'RESPOND TYPE=error MSG="{msg}"')
            except (MoonrakerError, aiohttp.ClientError, asyncio.TimeoutError) as e:
                self.log.debug("%s: console message failed: %s", mr.printer.name, e)
        hook = self.cfg.notify.discord_webhook
        if not hook:
            return
        embed = {"title": title, "description": text, "color": color}
        embeds = [embed]
        form = aiohttp.FormData()
        if snap is not None:
            embed["image"] = {"url": "attachment://snapshot.jpg"}
            form.add_field("files[0]", snap.jpeg, filename="snapshot.jpg",
                           content_type="image/jpeg")
        if before is not None:
            embeds.append({"description": "A few minutes earlier", "color": color,
                           "image": {"url": "attachment://before.jpg"}})
            form.add_field("files[1]", before, filename="before.jpg",
                           content_type="image/jpeg")
        form.add_field("payload_json", json.dumps({"username": "Failure detection",
                                                   "embeds": embeds}),
                       content_type="application/json")
        try:
            async with self.session.post(hook, data=form,
                                         timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status >= 300:
                    self.log.warning("Discord webhook: HTTP %s %s", resp.status,
                                     (await resp.text())[:200])
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            self.log.warning("Discord webhook failed: %s", e)


class Detector:
    """One printer: polls print state, grabs snapshots, scores them, alerts/pauses."""

    def __init__(self, printer: Printer, cfg: Config, session: aiohttp.ClientSession,
                 model: Model, store: PredictionStore, notifier: Notifier,
                 fault_model: faults.FaultModel | None = None):
        self.printer = printer
        self.cfg = cfg
        self.session = session
        self.model = model
        self.store = store
        self.notifier = notifier
        self.mr = Moonraker(printer, session)
        self.log = logging.getLogger(f"{printer.name}.detect")
        self.pred: Prediction = store.get(printer.name)
        self.state = ""
        self.filename = ""
        self.progress = 0.0
        self.online = False
        self.error = ""
        self.cams: list[dict] = []
        self.cams_at = 0.0
        self.snap: Snapshot | None = None
        self.history: deque = deque(maxlen=HISTORY)
        self.ignored = False
        self.warned = False
        self.failed = False
        self.paused_by_us = False
        self.events: deque = deque(maxlen=20)
        self.infer_seconds = 0.0
        self.motion = motion.MotionWatch()
        self.moved = False
        self.pos: tuple[float, float] | None = None
        # Problem-type model (trial): current boxes, checks in a row per type, types alerted
        self.faults = fault_model
        self.fault_boxes: list[dict] = []
        self.fault_streak: dict[str, int] = {}
        self.fault_alerted: set[str] = set()
        self.fault_seconds = 0.0
        self.print_started = 0.0    # when the current (or last) print started; 0 = unknown
        self.wake = asyncio.Event()

    # --- printer access ---

    async def webcams(self, refresh: bool = False) -> list[dict]:
        if refresh or not self.cams or time.monotonic() - self.cams_at > WEBCAM_REFRESH:
            self.cams = (await self.mr.get("/server/webcams/list"))["webcams"]
            self.cams_at = time.monotonic()
        return self.cams

    async def fetch_snapshot(self) -> Snapshot:
        cam = pick_webcam(await self.webcams(), self.printer.webcam)
        if cam is None:
            raise MoonrakerError("no webcam with snapshots" + (
                f" named {self.printer.webcam!r}" if self.printer.webcam else ""))
        async with self.session.get(snapshot_url(self.printer, cam), headers=self.mr.headers,
                                    timeout=SNAPSHOT_TIMEOUT) as resp:
            resp.raise_for_status()
            data = await resp.read()
        if not data.startswith(b"\xff\xd8"):
            raise MoonrakerError("webcam snapshot is not a JPEG")
        return Snapshot(jpeg=data, taken=time.time())

    async def poll_state(self) -> None:
        stats = await self.mr.get("/printer/objects/query", print_stats="state,filename,total_duration",
                                  virtual_sdcard="progress", toolhead="position")
        st = stats["status"]
        pos = st.get("toolhead", {}).get("position") or []
        self.pos = (float(pos[0]), float(pos[1])) if len(pos) >= 2 else None
        new = st.get("print_stats", {}).get("state", "")
        self.filename = st.get("print_stats", {}).get("filename", "") or self.filename
        self.progress = float(st.get("virtual_sdcard", {}).get("progress") or 0)
        self.print_duration = float(st.get("print_stats", {}).get("total_duration") or 0)
        old, self.state = self.state, new
        if old == new:
            return
        self.log.info("print state %s -> %s", old or "unknown", new)
        if new == "printing" and old not in ACTIVE:
            self.new_print()
        elif new == "printing" and old == "paused":
            # Things may have been moved by hand while paused: new reference.
            self.motion.reset()
            if self.warned or self.failed or self.moved:
                # Resumed after an alert: the person looked and decided to carry on.
                self.pred.reset_print()
                self.warned = self.failed = self.paused_by_us = self.moved = False
                self.fault_streak.clear()
                self.fault_alerted.clear()
                self.event("resumed", "Resumed; watching again after a short grace period")

    def new_print(self) -> None:
        # Klipper's total_duration counts from the real start, also when this app
        # (re)started in the middle of a print
        self.print_started = time.time() - getattr(self, "print_duration", 0.0)
        self.pred.reset_print()
        self.history.clear()
        self.ignored = self.warned = self.failed = self.paused_by_us = self.moved = False
        self.motion.reset()
        self.fault_boxes, self.fault_streak, self.fault_alerted = [], {}, set()
        self.event("start", f"Print started: {self.filename}")

    def event(self, kind: str, text: str) -> None:
        self.events.appendleft({"t": time.time(), "kind": kind, "text": text})

    # --- loop ---

    async def run(self) -> None:
        while True:
            try:
                await self.poll_state()
                self.online = True
                if self.state == "printing" and self.printer.detect != "off":
                    await self.check()
                    self.error = ""
                wait = self.cfg.detection.interval if self.state in ACTIVE else IDLE_POLL
            except asyncio.CancelledError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError, MoonrakerError, OSError) as e:
                msg = str(e) or type(e).__name__
                if msg != self.error:
                    self.log.warning("%s", msg)
                self.error = msg
                self.online = not isinstance(e, (aiohttp.ClientConnectorError,
                                                 asyncio.TimeoutError))
                wait = IDLE_POLL
            except Exception as e:
                self.log.exception("detector error")
                self.error = str(e)
                wait = IDLE_POLL
            self.wake.clear()
            try:
                await asyncio.wait_for(self.wake.wait(), wait)
            except asyncio.TimeoutError:
                pass

    async def check(self) -> None:
        snap = await self.fetch_snapshot()
        boxes, size, secs = await self.model.detect(snap.jpeg)
        snap.boxes, snap.size = boxes, size
        self.snap = snap
        self.infer_seconds = secs
        self.pred.update(sum(b["conf"] for b in boxes))
        self.store.put(self.printer.name, self.pred)
        sens = self.printer.sensitivity
        score = self.pred.normalized(sens)
        self.history.append((round(snap.taken), round(score, 3)))
        mres = None
        if self.printer.motion:
            was = self.motion.last.state
            mres = await asyncio.to_thread(self.motion.update, snap.jpeg, snap.taken, size,
                                           self.printer.motion_sensitivity, self.pos)
            if mres.note:
                self.log.info("motion: %s", mres.note)
            if mres.state == "candidate" and was != "candidate":
                self.log.info("motion: possible move (area %.3f)", mres.area)
                self.event("motion", "Something changed on the bed; checking it stays")
        await self.check_faults(snap)
        if self.ignored:
            return
        for label, n in self.fault_streak.items():
            if n >= faults.ALERT_STREAK and label not in self.fault_alerted:
                self.fault_alerted.add(label)
                conf = max(b["conf"] for b in self.fault_boxes if b["label"] == label)
                name = training.LABELS.get(label, label)
                await self.alert(snap, "fault", f"Possible problem: {name}", 0xE8A33D, text=(
                    f"{self.filename or 'print'} at {self.progress * 100:.0f}%. The problem-type "
                    f"detector (trial) saw {name.lower()} on {n} checks in a row, now {conf * 100:.0f}%. "
                    "It only alerts; it never pauses a print."))
        if mres is not None and mres.state == "moved" and not self.moved:
            self.moved = True
            self.log.warning("part moved (area %.3f)", mres.area)
            before = self.motion.before_jpeg
            if self.printer.detect == "pause" and not self.paused_by_us:
                self.paused_by_us = True
                await self.pause(snap, "moved", "Print paused: a part moved or came loose",
                                 before)
            else:
                await self.alert(snap, "moved", "A part moved or came loose", 0xD83C3E,
                                 before)
            return
        failing = self.pred.failing(sens, ESCALATING_FACTOR)
        warning = self.pred.failing(sens)
        if failing and self.printer.detect == "pause" and not self.paused_by_us:
            self.paused_by_us = self.failed = self.warned = True
            await self.pause(snap)
        elif failing and not self.failed:
            self.failed = self.warned = True
            await self.alert(snap, "failure", "Print is likely failing", 0xD83C3E)
        elif warning and not self.warned:
            self.warned = True
            await self.alert(snap, "warning", "Possible print failure", 0xE8A33D)

    async def check_faults(self, snap: Snapshot) -> None:
        if self.faults is None or not self.faults.available:
            self.fault_boxes = []
            return
        try:
            boxes, self.fault_seconds = await self.faults.detect(snap.jpeg)
        except Exception as e:
            self.log.warning("problem-type model: %s", e)
            self.fault_boxes = []
            return
        self.fault_boxes = boxes
        hot = {b["label"] for b in boxes if b["conf"] >= faults.ALERT_CONF}
        self.fault_streak = {k: self.fault_streak.get(k, 0) + 1 for k in hot}
        for k, n in self.fault_streak.items():
            if n == 1:
                self.log.info("problem type %s above %.0f%%", k, faults.ALERT_CONF * 100)

    def save(self, kind: str, snap: Snapshot, before: bytes | None) -> None:
        folder = self.cfg.detection.flagged_dir / self.printer.name
        if before is not None:
            detect.save_flagged(folder, "before", Snapshot(jpeg=before, taken=snap.taken))
        detect.save_flagged(folder, kind, snap)

    async def alert(self, snap: Snapshot, kind: str, title: str, color: int,
                    before: bytes | None = None, text: str = "") -> None:
        self.log.warning("%s (score %.2f)", title.lower(), self.pred.normalized(
            self.printer.sensitivity))
        self.save(kind, snap, before)
        self.event(kind, title)
        await self.notifier.send(self.mr, f"{self.printer.name}: {title}",
                                 text or self.describe(kind), color, snap, before)

    async def pause(self, snap: Snapshot, kind: str = "paused",
                    title: str = "Print paused: likely failure",
                    before: bytes | None = None) -> None:
        self.save(kind, snap, before)
        try:
            await self.mr.post("/printer/print/pause")
            self.log.warning("paused the print (%s)", title.lower())
            self.event("paused", title)
        except (MoonrakerError, aiohttp.ClientError, asyncio.TimeoutError) as e:
            title = f"{title.split(': ', 1)[-1]}; pause FAILED ({e})"
            self.log.error("pause failed: %s", e)
            self.event("failure", f"Pause failed: {e}")
        await self.notifier.send(self.mr, f"{self.printer.name}: {title}",
                                 self.describe(kind), 0xD83C3E, snap, before)

    def describe(self, kind: str = "") -> str:
        where = f"{self.filename or 'print'} at {self.progress * 100:.0f}%"
        if kind == "moved":
            return (f"{where}. The picture changed on the bed and stayed changed; "
                    "compare with the earlier snapshot.")
        return (f"{where}, failure score "
                f"{self.pred.normalized(self.printer.sensitivity) * 100:.0f}%")

    # --- page actions ---

    def ignore(self) -> None:
        self.ignored = True
        self.event("ignored", "Marked as not a failure; no more alerts this print")
        self.motion.reset(keep_frames=True)

    def status(self) -> dict:
        p = self.printer
        sens = p.sensitivity
        score = self.pred.normalized(sens) if self.pred.current_frame_num else 0.0
        level = "ok"
        if self.state == "printing" and p.detect != "off":
            if self.pred.failing(sens, ESCALATING_FACTOR) or self.moved:
                level = "failing"
            elif self.pred.failing(sens):
                level = "warning"
            elif self.pred.current_frame_num < detect.INIT_SAFE_FRAME_NUM:
                level = "warming"
        # score_level: the spaghetti model alone; level also counts the trial problem-type alerts
        score_level = level
        if level in ("ok", "warming") and self.fault_alerted and not self.ignored                 and self.state == "printing" and p.detect != "off":
            level = "warning"
        return {
            "name": p.name, "host": p.host, "mode": p.mode, "detect": p.detect,
            "stream": stream_url(p, pick_webcam(self.cams, p.webcam)),
            "online": self.online, "error": self.error or self.model.error,
            "state": self.state, "filename": self.filename, "progress": self.progress,
            "score": round(score, 3), "level": level, "score_level": score_level,
            "print_started": self.print_started,
            "ignored": self.ignored,
            "frames": self.pred.current_frame_num,
            "safe_frames": detect.INIT_SAFE_FRAME_NUM,
            "snapshot": None if self.snap is None else {
                "t": self.snap.taken, "size": self.snap.size, "boxes": [
                    b for b in self.snap.boxes if b["conf"] >= detect.VISUALIZATION_THRESH]},
            "history": list(self.history),
            "events": list(self.events),
            "infer_ms": round(self.infer_seconds * 1000),
            "motion": None if not p.motion else {
                "state": "moved" if self.moved else self.motion.last.state,
                "score": round(min(self.motion.last.score, 9.99), 2),
                "cells": self.motion.last.cells if self.motion.last.score >= 0.5 else []},
            "faults": None if self.faults is None or not self.faults.available else {
                "boxes": [b for b in self.fault_boxes if b["conf"] >= faults.SHOW_CONF],
                "streak": self.fault_streak, "alerted": sorted(self.fault_alerted),
                "alert_conf": faults.ALERT_CONF, "alert_streak": faults.ALERT_STREAK,
                "ms": round(self.fault_seconds * 1000), "error": self.faults.error},
        }

    def flagged(self, this_print: bool = False) -> list[Path]:
        """Flagged frames, newest first; with this_print, only those since the current
        (or last) print started, when that is known."""
        folder = self.cfg.detection.flagged_dir / self.printer.name
        files = sorted(folder.glob("*.jpg"), reverse=True) if folder.exists() else []
        if this_print and self.print_started:
            since = datetime.fromtimestamp(self.print_started).strftime("%Y%m%d_%H%M%S")
            files = [f for f in files if f.name[:15] >= since]
        return files
