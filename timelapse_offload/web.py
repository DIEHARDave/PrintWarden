"""Monitor + settings web page (aiohttp)."""

import asyncio
import ipaddress
import logging
import re
import time
from importlib import resources

import aiohttp
from aiohttp import web

from . import config
from .detect import Snapshot
from .moonraker import MoonrakerError

log = logging.getLogger("web")
SCAN_TIMEOUT = aiohttp.ClientTimeout(total=4)
SCAN_MAX_HOSTS = 1024
FILE_RE = re.compile(r"^[0-9_]+_[a-z]+\.jpg$")


def _detector(request: web.Request):
    det = request.app["sup"].detector(request.match_info["name"])
    if det is None:
        raise web.HTTPNotFound(text="no such printer")
    return det


def _page(name: str) -> web.Response:
    html = resources.files(__package__).joinpath(f"web/{name}").read_text(encoding="utf-8")
    return web.Response(text=html, content_type="text/html",
                        headers={"Cache-Control": "no-cache"})


async def monitor(request: web.Request) -> web.Response:
    """Printer Monitor (github.com/DIEHARDave/printer-monitor, copied into web/monitor.html):
    live cameras, screens, cards and spools, with Print Watch's boxes drawn over the video."""
    return _page("monitor.html")


async def to_watch(request: web.Request) -> web.Response:
    raise web.HTTPFound("/watch/")


async def index(request: web.Request) -> web.Response:
    """Print Watch: detection details and settings."""
    return _page("index.html")


async def status(request: web.Request) -> web.Response:
    sup = request.app["sup"]
    # The live stream URL comes from the webcam list, which is only fetched with a
    # snapshot otherwise; load it for online printers that have not had one yet.
    dets = [d for d in (sup.detector(p.name) for p in sup.cfg.printers)
            if d is not None and d.online and not d.cams]
    if dets:
        await asyncio.gather(*(asyncio.wait_for(d.webcams(), 3) for d in dets),
                             return_exceptions=True)
    printers = []
    for p in sup.cfg.printers:
        det = sup.detector(p.name)
        if det is None:
            continue
        s = det.status()
        s["flagged"] = [f.name for f in det.flagged()[:12]]
        printers.append(s)
    return web.json_response({"printers": printers, "interval": sup.cfg.detection.interval,
                              "model_ready": sup.model._session is not None,
                              "model_error": sup.model.error})


async def snapshot(request: web.Request) -> web.Response:
    """The last detection frame, or a fresh one from the camera with ?live=1."""
    det = _detector(request)
    snap: Snapshot | None = det.snap
    if request.query.get("live") or snap is None:
        try:
            snap = await det.fetch_snapshot()
        except (MoonrakerError, aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
            raise web.HTTPBadGateway(text=f"snapshot failed: {e or type(e).__name__}")
    return web.Response(body=snap.jpeg, content_type="image/jpeg",
                        headers={"Cache-Control": "no-store",
                                 "X-Snapshot-Time": f"{snap.taken:.0f}"})


async def ignore(request: web.Request) -> web.Response:
    _detector(request).ignore()
    return web.json_response({"ok": True})


async def webcams(request: web.Request) -> web.Response:
    """Webcam names with snapshots on any host (the printer may not be saved yet)."""
    sup = request.app["sup"]
    host = request.query.get("host", "").strip()
    try:
        port = int(request.query.get("port") or 7125)
    except ValueError:
        port = 0
    if not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", host) or not 0 < port < 65536:
        raise web.HTTPBadRequest(text="bad host or port")
    try:
        async with sup.session.get(f"http://{host}:{port}/server/webcams/list",
                                   timeout=SCAN_TIMEOUT) as resp:
            resp.raise_for_status()
            cams = (await resp.json(content_type=None))["result"]["webcams"]
    except (aiohttp.ClientError, asyncio.TimeoutError, KeyError, ValueError, OSError) as e:
        raise web.HTTPBadGateway(text=str(e) or type(e).__name__)
    return web.json_response([c.get("name") for c in cams
                              if c.get("snapshot_url") and c.get("service") != "iframe"])


async def flagged(request: web.Request) -> web.Response:
    det = _detector(request)
    name = request.match_info["file"]
    if not FILE_RE.match(name):
        raise web.HTTPNotFound()
    path = det.cfg.detection.flagged_dir / det.printer.name / name
    if not path.is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(path)


async def get_settings(request: web.Request) -> web.Response:
    cfg = request.app["sup"].cfg
    return web.json_response(config.settings_dict(cfg))


async def put_settings(request: web.Request) -> web.Response:
    sup = request.app["sup"]
    try:
        data = await request.json()
        config.save_settings(sup.cfg, data)
    except (ValueError, TypeError) as e:
        return web.json_response({"error": str(e)}, status=400)
    sup.apply()
    log.info("settings saved from the web page")
    return web.json_response(config.settings_dict(sup.cfg))


async def probe(session: aiohttp.ClientSession, host: str, port: int) -> dict | None:
    try:
        async with session.get(f"http://{host}:{port}/printer/info",
                               timeout=SCAN_TIMEOUT) as resp:
            if resp.status not in (200, 503):
                return None
            body = await resp.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, OSError):
        return None
    if not isinstance(body, dict) or not ("result" in body or "error" in body):
        return None
    info = body.get("result") or {}
    return {"host": host, "port": port, "hostname": info.get("hostname", ""),
            "state": info.get("state", "unknown")}


async def scan(request: web.Request) -> web.Response:
    """Look for Moonraker on port 7125 across a subnet."""
    sup = request.app["sup"]
    data = await request.json()
    try:
        net = ipaddress.ip_network(str(data.get("subnet", "")).strip(), strict=False)
    except ValueError:
        return web.json_response({"error": "subnet must look like 10.0.1.0/24"}, status=400)
    if net.version != 4 or net.num_addresses > SCAN_MAX_HOSTS:
        return web.json_response({"error": "use an IPv4 subnet of /22 or smaller"}, status=400)
    port = int(data.get("port") or 7125)
    sem = asyncio.Semaphore(128)

    async def one(ip):
        async with sem:
            return await probe(sup.session, str(ip), port)

    started = time.monotonic()
    found = [r for r in await asyncio.gather(*(one(ip) for ip in net.hosts())) if r]
    known = {p.host for p in sup.cfg.printers}
    for r in found:
        r["known"] = r["host"] in known
    log.info("scan of %s found %d Moonraker host(s) in %.1fs", net, len(found),
             time.monotonic() - started)
    return web.json_response({"found": found})


async def test_notify(request: web.Request) -> web.Response:
    sup = request.app["sup"]
    if not sup.cfg.notify.discord_webhook:
        return web.json_response({"error": "save a Discord webhook first"}, status=400)
    det = next((d for d, _ in sup.detectors.values()), None)
    snap = det.snap if det else None
    await sup.notifier.send(det.mr, "Test from TimelapseOffload",
                            "Failure-detection alerts will look like this.", 0x3D8BE8, snap)
    return web.json_response({"ok": True})


async def start(sup) -> web.AppRunner:
    app = web.Application()
    app["sup"] = sup
    app.add_routes([
        web.get("/", monitor),
        web.get("/watch", to_watch),
        web.get("/watch/", index),
        web.get("/api/status", status),
        web.get("/api/printers/{name}/snapshot.jpg", snapshot),
        web.post("/api/printers/{name}/ignore", ignore),
        web.get("/api/webcams", webcams),
        web.get("/api/printers/{name}/flagged/{file}", flagged),
        web.get("/api/settings", get_settings),
        web.put("/api/settings", put_settings),
        web.post("/api/scan", scan),
        web.post("/api/test-notify", test_notify),
    ])
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", sup.cfg.web.port).start()
    log.info("web page on port %d", sup.cfg.web.port)
    return runner
