"""Minimal Moonraker HTTP + websocket client."""

import asyncio
import itertools
import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aiohttp

from .config import Printer

FRAMES_ROOT = "timelapse_frames"
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=60)
# Finished videos can be tens of MB; cap stalls, not total transfer time.
FILE_TIMEOUT = aiohttp.ClientTimeout(sock_connect=15, sock_read=120)
DOWNLOAD_RETRIES = 3


class MoonrakerError(Exception):
    pass


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

        # Close any gaps left by skipped frames so ffmpeg's frame%06d sees a run.
        got = sorted(dest.glob("frame*.jpg"))
        if len(got) < len(frames):
            self.log.warning("%d of %d frames unavailable; rendering the rest",
                             len(frames) - len(got), len(frames))
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
