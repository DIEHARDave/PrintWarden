"""Labelled frames for training better failure detection.

A frame comes from a printer's camera (the camera button on Printer Monitor) or
from a flagged frame, and is stored in <data>/training as <id>.jpg with an
<id>.json next to it: printer, time, size, verdict, boxes [{x, y, w, h, label}]
in image pixels, and a note. /api/training/export.zip packs them as a YOLO
dataset (images/, labels/, classes.txt) plus the raw JSON.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import re
import secrets
import time
import zipfile
from pathlib import Path

import aiohttp
from aiohttp import web

from .moonraker import MoonrakerError

log = logging.getLogger(__name__)

# Problem types offered on the label page, in this order (YOLO class ids follow it)
LABELS = {
    "spaghetti": "Spaghetti / loose strands",
    "detached": "Part came loose or moved",
    "layer_shift": "Layer shift",
    "warping": "Warping / lifting",
    "stringing": "Stringing",
    "under_extrusion": "Under-extrusion / gaps",
    "over_extrusion": "Over-extrusion",
    "blob": "Blob on nozzle / zits",
    "cracks": "Cracks / layer splitting",
    "other": "Other problem",
}
VERDICTS = ("problem", "fine", "unsure")
ID_RE = re.compile(r"^\d{8}_\d{6}_[0-9a-f]{6}$")
MAX_BOXES = 100


def folder(sup) -> Path:
    return sup.cfg.detection.flagged_dir.parent / "training"


def _paths(sup, sid: str) -> tuple[Path, Path]:
    if not ID_RE.match(sid):
        raise web.HTTPNotFound()
    base = folder(sup) / sid
    return base.with_suffix(".jpg"), base.with_suffix(".json")


def _read(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    tmp.replace(path)


def _new(sup, jpeg: bytes, meta: dict) -> dict:
    d = folder(sup)
    d.mkdir(parents=True, exist_ok=True)
    sid = time.strftime("%Y%m%d_%H%M%S") + "_" + secrets.token_hex(3)
    img, js = _paths(sup, sid)
    img.write_bytes(jpeg)
    data = {"id": sid, "verdict": None, "boxes": [], "note": "", **meta}
    _write(js, data)
    return data


def _jpeg_size(jpeg: bytes) -> tuple[int, int]:
    from PIL import Image
    with Image.open(io.BytesIO(jpeg)) as im:
        return im.size


async def capture(request: web.Request) -> web.Response:
    """Take a fresh camera frame from a printer for labelling."""
    sup = request.app["sup"]
    body = await request.json()
    det = sup.detector(str(body.get("printer", "")))
    if det is None:
        raise web.HTTPNotFound(text="no such printer")
    try:
        snap = await det.fetch_snapshot()
    except (MoonrakerError, aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
        raise web.HTTPBadGateway(text=f"snapshot failed: {e or type(e).__name__}")
    size = snap.size if snap.size[0] else _jpeg_size(snap.jpeg)
    data = _new(sup, snap.jpeg, {
        "printer": det.printer.name, "source": "camera", "taken": snap.taken, "size": list(size),
        "state": det.state, "filename": det.filename, "progress": det.progress})
    log.info("%s: frame %s captured for training", det.printer.name, data["id"])
    return web.json_response(data)


async def from_flagged(request: web.Request) -> web.Response:
    """Copy a flagged frame (an alert's snapshot) so it can be labelled, e.g. as a false alarm."""
    sup = request.app["sup"]
    body = await request.json()
    det = sup.detector(str(body.get("printer", "")))
    name = str(body.get("file", ""))
    if det is None or not re.fullmatch(r"[\w.-]+\.jpg", name):
        raise web.HTTPNotFound()
    src = det.cfg.detection.flagged_dir / det.printer.name / name
    if not src.is_file():
        raise web.HTTPNotFound(text="that flagged frame is gone")
    jpeg = src.read_bytes()
    data = _new(sup, jpeg, {
        "printer": det.printer.name, "source": f"flagged/{name}", "taken": src.stat().st_mtime,
        "size": list(_jpeg_size(jpeg))})
    return web.json_response(data)


async def get_one(request: web.Request) -> web.Response:
    sup = request.app["sup"]
    img, js = _paths(sup, request.match_info["id"])
    if not js.is_file():
        raise web.HTTPNotFound()
    return web.json_response(_read(js))


async def image(request: web.Request) -> web.Response:
    img, _ = _paths(request.app["sup"], request.match_info["id"])
    if not img.is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(img, headers={"Cache-Control": "private, max-age=3600"})


async def save(request: web.Request) -> web.Response:
    sup = request.app["sup"]
    img, js = _paths(sup, request.match_info["id"])
    if not js.is_file():
        raise web.HTTPNotFound()
    data = _read(js)
    body = await request.json()
    w, h = data.get("size") or (0, 0)
    boxes = []
    for b in (body.get("boxes") or [])[:MAX_BOXES]:
        try:
            x, y, bw, bh = (float(b[k]) for k in ("x", "y", "w", "h"))
        except (KeyError, TypeError, ValueError):
            return web.json_response({"error": "bad box"}, status=400)
        label = str(b.get("label", ""))
        if label not in LABELS:
            return web.json_response({"error": f"unknown problem type {label!r}"}, status=400)
        # Clamp to the image; drop slivers
        x, y = max(0.0, min(x, w)), max(0.0, min(y, h))
        bw, bh = max(0.0, min(bw, w - x)), max(0.0, min(bh, h - y))
        if bw >= 3 and bh >= 3:
            boxes.append({"x": round(x, 1), "y": round(y, 1), "w": round(bw, 1), "h": round(bh, 1), "label": label})
    verdict = body.get("verdict")
    if verdict not in VERDICTS:
        verdict = "problem" if boxes else None
    data.update(boxes=boxes, verdict=verdict, note=str(body.get("note", ""))[:2000],
                labelled=time.time())
    _write(js, data)
    log.info("training frame %s labelled: %s, %d box(es)", data["id"], verdict, len(boxes))
    return web.json_response(data)


async def delete(request: web.Request) -> web.Response:
    img, js = _paths(request.app["sup"], request.match_info["id"])
    img.unlink(missing_ok=True)
    js.unlink(missing_ok=True)
    return web.json_response({"ok": True})


async def listing(request: web.Request) -> web.Response:
    d = folder(request.app["sup"])
    items = [_read(p) for p in sorted(d.glob("*.json"), reverse=True)] if d.exists() else []
    items = [i for i in items if i.get("id")]
    return web.json_response({"labels": LABELS, "verdicts": list(VERDICTS), "items": items})


async def labels(request: web.Request) -> web.Response:
    return web.json_response({"labels": LABELS, "verdicts": list(VERDICTS)})


def _export(d: Path) -> bytes:
    names = list(LABELS)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        z.writestr("classes.txt", "\n".join(names) + "\n")
        z.writestr("README.txt",
                   "Frames labelled on Printer Monitor.\n"
                   "images/ + labels/: YOLO format (class cx cy w h, 0..1), classes in classes.txt.\n"
                   "A frame marked 'fine' has an empty label file (a negative example).\n"
                   "json/: the full record per frame (verdict, note, printer, source, boxes in pixels).\n")
        for js in sorted(d.glob("*.json")):
            data = _read(js)
            img = js.with_suffix(".jpg")
            if not data.get("verdict") or not img.is_file():
                continue   # not labelled yet
            w, h = data.get("size") or (0, 0)
            if not w or not h:
                continue
            lines = [f"{names.index(b['label'])} {(b['x'] + b['w'] / 2) / w:.6f} {(b['y'] + b['h'] / 2) / h:.6f} "
                     f"{b['w'] / w:.6f} {b['h'] / h:.6f}" for b in data.get("boxes", []) if b.get("label") in names]
            z.write(img, f"images/{js.stem}.jpg")
            z.writestr(f"labels/{js.stem}.txt", "\n".join(lines) + ("\n" if lines else ""))
            z.writestr(f"json/{js.stem}.json", json.dumps(data, indent=1))
    return buf.getvalue()


async def export(request: web.Request) -> web.Response:
    d = folder(request.app["sup"])
    body = await asyncio.to_thread(_export, d) if d.exists() else _export_empty()
    return web.Response(body=body, content_type="application/zip", headers={
        "Content-Disposition": f'attachment; filename="training-{time.strftime("%Y%m%d")}.zip"'})


def _export_empty() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("classes.txt", "\n".join(LABELS) + "\n")
    return buf.getvalue()


def routes() -> list:
    return [
        web.get("/api/training", listing),
        web.get("/api/training/labels", labels),
        web.get("/api/training/export.zip", export),
        web.post("/api/training/capture", capture),
        web.post("/api/training/from-flagged", from_flagged),
        web.get("/api/training/{id}.jpg", image),
        web.get("/api/training/{id}", get_one),
        web.put("/api/training/{id}", save),
        web.delete("/api/training/{id}", delete),
    ]
