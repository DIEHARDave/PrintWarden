"""Problem-type detector: our own YOLO model (tools/train_detector.py) that boxes
spaghetti, stringing, under/over-extrusion, warping, blobs, cracks and loose parts.

It runs next to the Obico spaghetti model, on the same snapshots. Trial mode:
it only alerts, after a type stays above ALERT_CONF for ALERT_STREAK checks in a
row, once per type per print, and never pauses. If <model_dir>/faults.onnx is
missing it is simply off.
"""

from __future__ import annotations

import ast
import asyncio
import io
import logging
import time
from pathlib import Path

import numpy as np
from PIL import Image

from .detect import nms

log = logging.getLogger(__name__)

MODEL_FILE = "faults.onnx"
BOX_CONF = 0.25        # lowest box kept from the model
SHOW_CONF = 0.35       # drawn on the page
ALERT_CONF = 0.5       # counts towards an alert
ALERT_STREAK = 3       # checks in a row before alerting (30 s at 10 s)
NMS_IOU = 0.5


class FaultModel:
    """ONNX YOLO (Ultralytics export, output [1, 4 + classes, anchors]). One inference at a time."""

    def __init__(self, model_dir: Path, threads: int):
        self.path = model_dir / MODEL_FILE
        self.threads = threads
        self._session = None
        self._mtime = 0.0
        self._lock = asyncio.Lock()
        self.names: dict[int, str] = {}
        self.error = ""

    @property
    def available(self) -> bool:
        return self.path.is_file()

    def _load(self):
        import onnxruntime as ort
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = self.threads
        opts.inter_op_num_threads = 1
        sess = ort.InferenceSession(str(self.path), sess_options=opts,
                                    providers=["CPUExecutionProvider"])
        meta = sess.get_modelmeta().custom_metadata_map
        self.names = {int(k): str(v) for k, v in ast.literal_eval(meta.get("names", "{}")).items()}
        log.info("problem-type model loaded: %s (%s)", self.path.name, ", ".join(self.names.values()))
        return sess

    async def detect(self, jpeg: bytes) -> tuple[list[dict], float]:
        """Boxes {label, conf, x, y, w, h} in snapshot pixels, and seconds taken."""
        if not self.available:
            return [], 0.0
        async with self._lock:
            mtime = self.path.stat().st_mtime
            if self._session is None or mtime != self._mtime:   # a new model file was put in place
                try:
                    self._session = await asyncio.to_thread(self._load)
                    self._mtime = mtime
                    self.error = ""
                except Exception as e:
                    self._session = None
                    self.error = f"problem-type model unavailable: {e}"
                    raise
            start = time.monotonic()
            boxes = await asyncio.to_thread(self._infer, jpeg)
            return boxes, time.monotonic() - start

    def _infer(self, jpeg: bytes) -> list[dict]:
        img = Image.open(io.BytesIO(jpeg)).convert("RGB")
        w0, h0 = img.size
        inp = self._session.get_inputs()[0]
        size = int(inp.shape[2]) if isinstance(inp.shape[2], int) else 640
        # Letterbox like Ultralytics: keep the shape, pad with grey
        scale = min(size / w0, size / h0)
        nw, nh = round(w0 * scale), round(h0 * scale)
        px, py = (size - nw) // 2, (size - nh) // 2
        canvas = Image.new("RGB", (size, size), (114, 114, 114))
        canvas.paste(img.resize((nw, nh), Image.BILINEAR), (px, py))
        arr = np.asarray(canvas, dtype=np.float32).transpose(2, 0, 1)[None] / 255.0
        out = self._session.run(None, {inp.name: arr})[0][0].T      # [anchors, 4 + classes]
        scores = out[:, 4:]
        cls = scores.argmax(axis=1)
        conf = scores[np.arange(len(cls)), cls]
        keep = conf >= BOX_CONF
        if not keep.any():
            return []
        b, conf, cls = out[keep, :4], conf[keep], cls[keep]
        xyxy = np.stack([b[:, 0] - b[:, 2] / 2, b[:, 1] - b[:, 3] / 2,
                         b[:, 0] + b[:, 2] / 2, b[:, 1] + b[:, 3] / 2], axis=1)
        # Per-class NMS: shift each class far apart so boxes of different types never suppress
        idx = nms(xyxy + cls[:, None] * 4096.0, conf, NMS_IOU)
        boxes = []
        for i in idx[:30]:
            x1, y1, x2, y2 = (float(v) for v in xyxy[i])   # numpy float32 breaks JSON
            x1, x2 = [min(w0, max(0.0, (v - px) / scale)) for v in (x1, x2)]
            y1, y2 = [min(h0, max(0.0, (v - py) / scale)) for v in (y1, y2)]
            if x2 - x1 < 2 or y2 - y1 < 2:
                continue
            boxes.append({"label": self.names.get(int(cls[i]), str(int(cls[i]))), "conf": round(float(conf[i]), 3),
                          "x": round(x1, 1), "y": round(y1, 1), "w": round(x2 - x1, 1), "h": round(y2 - y1, 1)})
        return boxes
