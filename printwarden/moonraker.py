"""Minimal Moonraker HTTP + websocket client."""

import asyncio
import itertools
import json
import logging
import re
import shutil
import zipfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aiohttp

from .config import Printer

FRAMES_ROOT = "timelapse_frames"
# moonraker-timelapse's output folder; with "Save Frames" on it holds a
# <video name>_frames.zip per completed print.
VIDEOS_ROOT = "timelapse"
SAVED_FRAMES_SUFFIX = "_frames.zip"
FRAME_RE = re.compile(r"frame\d+\.jpg")
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=60)
# Finished videos can be tens of MB; cap stalls, not total transfer time.
FILE_TIMEOUT = aiohttp.ClientTimeout(sock_connect=15, sock_read=120)
DOWNLOAD_RETRIES = 3
# Building a zip of ~1000 frames on a slow eMMC can take a while.
ZIP_TIMEOUT = aiohttp.ClientTimeout(total=900)
ZIP_FREE_MARGIN = 50 * 1024 * 1024


class MoonrakerError(Exception):
    pass


def extract_frames(archive: Path, dest: Path) -> int:
    """Unpack frame*.jpg from a zip as frame000001.jpg.. in name order, with no gaps.

    Handles both Moonraker's /server/files/zip ("timelapse_frames/frameN.jpg")
    and moonraker-timelapse's saved-frames zip (bare "frameN.jpg").
    Returns the number of frames written.
    """
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as z:
        members = sorted((i for i in z.infolist()
                          if FRAME_RE.fullmatch(i.filename.rsplit("/", 1)[-1])),
                         key=lambda i: i.filename.rsplit("/", 1)[-1])
        for n, info in enumerate(members, 1):
            with z.open(info) as src, (dest / f"frame{n:06d}.jpg").open("wb") as out:
                shutil.copyfileobj(src, out)
    return len(members)


class Moonraker:
    def __init__(self, printer: Printer, session: aiohttp.ClientSession):
        self.printer = printer
        self.session = session
        self.log = logging.getLogger(printer.name)
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._ws: aiohttp.ClientWebSocketResponse | None = None

    @property
    def headers(self) -> dict[str, str]:
        return {"X-Api-Key": self.printer.api_key} if self.printer.api_key else {}

    # --- HTTP ---

    async def get(self, path: str, **params) -> Any:
        return await self._request("GET", path, params)

    async def post(self, path: str, **params) -> Any:
        return await self._request("POST", path, params)

    async def _request(self, method: str, path: str, params: dict) -> Any:
        url = self.printer.base_url + path
        async with self.session.request(method, url, params=params, headers=self.headers,
                                        timeout=HTTP_TIMEOUT) as resp:
            body = await resp.json(content_type=None)
            if resp.status != 200:
                msg = (body or {}).get("error", {}).get("message", resp.reason)
                raise MoonrakerError(f"{method} {path}: {resp.status} {msg}")
            return body["result"]

    async def timelapse_settings(self) -> dict | None:
        """moonraker-timelapse settings, or None if the component isn't installed."""
        try:
            settings = await self.get("/machine/timelapse/settings")
        except MoonrakerError as e:
            if " 404 " in str(e):
                return None
            raise
        # Snapmaker's stock firmware answers this path with just {"enabled": false}.
        return settings if "autorender" in settings else None

    async def list_root(self, root: str) -> list[dict]:
        """All files under a Moonraker file root (e.g. 'timelapse')."""
        return await self.get("/server/files/list", root=root)

    async def download_file(self, root: str, path: str, dest: Path) -> None:
        """Stream one file from a Moonraker root to dest (via a .part temp)."""
        url = f"{self.printer.base_url}/server/files/{root}/{path}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        async with self.session.get(url, headers=self.headers, timeout=FILE_TIMEOUT) as resp:
            resp.raise_for_status()
            with tmp.open("wb") as f:
                async for chunk in resp.content.iter_chunked(1 << 20):
                    f.write(chunk)
        tmp.replace(dest)

    async def delete_file(self, root: str, path: str) -> None:
        url = f"{self.printer.base_url}/server/files/{root}/{path}"
        async with self.session.delete(url, headers=self.headers, timeout=HTTP_TIMEOUT) as resp:
            if resp.status != 200:
                body = await resp.json(content_type=None)
                msg = (body or {}).get("error", {}).get("message", resp.reason)
                raise MoonrakerError(f"delete {root}/{path}: {resp.status} {msg}")

    async def frames_delete_path(self) -> tuple[str, str] | None:
        """A writable root (and prefix in it) through which the frames can be deleted.

        moonraker-timelapse registers timelapse_frames read-only, but on some
        setups (Zmod: gcodes/timelapse/tmp) its folder sits inside a writable
        root, so the frames can be deleted through that one. None if not.
        """
        roots = await self.get("/server/files/roots")
        frames = next((r for r in roots if r["name"] == FRAMES_ROOT), None)
        if frames is None:
            return None
        if "w" in frames.get("permissions", ""):
            return FRAMES_ROOT, ""
        target = frames["path"].rstrip("/") + "/"
        parents = [r for r in roots if "w" in r.get("permissions", "")
                   and target.startswith(r["path"].rstrip("/") + "/")]
        if not parents:
            return None
        parent = max(parents, key=lambda r: len(r["path"]))
        return parent["name"], target[len(parent["path"].rstrip("/")) + 1:]

    async def delete_frames(self, frames: list[dict], concurrency: int) -> int:
        """Delete the given frame files from the printer. Returns how many went.

        Goes through a writable root that contains the frames folder if there
        is one. Otherwise the frames root is read-only and the API rejects
        deletes (405), so we don't try; the plugin clears the frames itself
        when the next print starts.
        """
        found = await self.frames_delete_path()
        if found is None:
            self.log.info("frames folder is read-only on this printer; the plugin "
                          "clears it at the next print start")
            return 0
        root, prefix = found
        sem = asyncio.Semaphore(concurrency)
        deleted = 0
        blocked = False

        async def rm(frame: dict) -> None:
            nonlocal deleted, blocked
            if blocked:
                return
            async with sem:
                if blocked:
                    return
                try:
                    await self.delete_file(root, prefix + frame["path"])
                    deleted += 1
                except MoonrakerError as e:
                    if "405" in str(e):
                        blocked = True
                    else:
                        self.log.warning("could not delete %s: %s", frame["path"], e)

        await asyncio.gather(*(rm(f) for f in frames))
        if blocked:
            self.log.warning("frames root is read-only on this printer; cannot delete "
                             "via API (the plugin clears them at the next print start)")
        return deleted

    async def list_frames(self) -> list[dict]:
        files = await self.get("/server/files/list", root=FRAMES_ROOT)
        frames = [f for f in files if f["path"].startswith("frame") and f["path"].endswith(".jpg")]
        return sorted(frames, key=lambda f: f["path"])

    async def download_frames(self, frames: list[dict], dest: Path, concurrency: int) -> int:
        """Download frames to dest, then renumber frame000001.jpg.. with no gaps.

        A frame the printer lists but can't serve (404) or that keeps failing is
        skipped, not fatal: one missing frame shouldn't lose the whole timelapse.
        Returns the number of frames actually downloaded.
        """
        dest.mkdir(parents=True, exist_ok=True)
        sem = asyncio.Semaphore(concurrency)

        async def fetch(index: int, frame: dict) -> None:
            url = f"{self.printer.base_url}/server/files/{FRAMES_ROOT}/{frame['path']}"
            target = dest / f"frame{index:06d}.jpg"
            async with sem:
                for attempt in range(1, DOWNLOAD_RETRIES + 1):
                    try:
                        async with self.session.get(url, headers=self.headers,
                                                    timeout=HTTP_TIMEOUT) as resp:
                            if resp.status == 404:
                                self.log.warning("frame %s missing on printer, skipping",
                                                 frame["path"])
                                return
                            resp.raise_for_status()
                            target.write_bytes(await resp.read())
                        return
                    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                        if attempt == DOWNLOAD_RETRIES:
                            self.log.warning("frame %s failed after retries, skipping: %s",
                                             frame["path"], e)
                            return
                        await asyncio.sleep(2 * attempt)

        await asyncio.gather(*(fetch(i, f) for i, f in enumerate(frames, 1)))
        return self._close_gaps(dest, len(frames))

    async def download_frames_zip(self, frames: list[dict], dest: Path, zip_root: str) -> int:
        """Like download_frames, but via one uncompressed zip built by Moonraker.

        The zip is written to zip_root on the printer (the frames root itself is
        usually read-only), downloaded in one request, and deleted again.
        Raises MoonrakerError if the printer lacks space or can't build it.
        """
        need = sum(f.get("size", 0) for f in frames) + ZIP_FREE_MARGIN
        info = await self._request("GET", "/server/files/directory",
                                   {"path": zip_root, "extended": "false"})
        free = (info.get("disk_usage") or {}).get("free")
        if free is not None and free < need:
            raise MoonrakerError(f"only {free / 1e6:.0f} MB free in '{zip_root}', "
                                 f"zip needs {need / 1e6:.0f} MB")

        name = f"printwarden_{frames[0]['modified']:.0f}.zip"
        body = {"dest": f"{zip_root}/{name}", "store_only": True,
                "items": [f"{FRAMES_ROOT}/{f['path']}" for f in frames]}
        async with self.session.post(self.printer.base_url + "/server/files/zip", json=body,
                                     headers=self.headers, timeout=ZIP_TIMEOUT) as resp:
            reply = await resp.json(content_type=None)
            if resp.status != 200:
                msg = (reply or {}).get("error", {}).get("message", resp.reason)
                raise MoonrakerError(f"zip frames: {resp.status} {msg}")
        local = dest.parent / name
        try:
            await self.download_file(zip_root, name, local)
        finally:
            try:
                await self.delete_file(zip_root, name)
            except (MoonrakerError, aiohttp.ClientError, asyncio.TimeoutError) as e:
                self.log.warning("could not delete %s/%s from printer: %s", zip_root, name, e)
        try:
            got = await asyncio.to_thread(extract_frames, local, dest)
        finally:
            local.unlink(missing_ok=True)
        if got < len(frames):
            self.log.warning("%d of %d frames missing from zip; rendering the rest",
                             len(frames) - got, len(frames))
        return got

    def _close_gaps(self, dest: Path, expected: int) -> int:
        """Renumber frame*.jpg in dest so ffmpeg's frame%06d sees an unbroken run."""
        got = sorted(dest.glob("frame*.jpg"))
        if len(got) < expected:
            self.log.warning("%d of %d frames unavailable; rendering the rest",
                             expected - len(got), expected)
            for n, path in enumerate(got, 1):
                want = dest / f"frame{n:06d}.jpg"
                if path != want:
                    path.rename(want)
        return len(got)

    # --- websocket ---

    async def connect(self) -> None:
        self._ws = await self.session.ws_connect(self.printer.ws_url, headers=self.headers,
                                                 heartbeat=30)

    async def close(self) -> None:
        if self._ws is not None:
            await self._ws.close()
        for fut in self._pending.values():
            fut.cancel()
        self._pending.clear()

    async def call(self, method: str, **params) -> Any:
        """Send a JSON-RPC request; the reply is delivered by messages()."""
        req_id = next(self._ids)
        fut = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        await self._ws.send_json({"jsonrpc": "2.0", "method": method, "params": params,
                                  "id": req_id})
        return await asyncio.wait_for(fut, timeout=30)

    async def messages(self) -> AsyncIterator[dict]:
        """Yield notifications until the socket closes. Replies resolve call() instead."""
        async for msg in self._ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                if msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                    break
                continue
            data = json.loads(msg.data)
            fut = self._pending.pop(data.get("id"), None) if "id" in data else None
            if fut is not None:
                if "error" in data:
                    fut.set_exception(MoonrakerError(data["error"].get("message", data["error"])))
                else:
                    fut.set_result(data.get("result"))
            elif "method" in data:
                yield data
