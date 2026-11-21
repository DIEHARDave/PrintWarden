"""Print failure ("spaghetti") detection on webcam snapshots.

Uses Obico's open-source failure-detection model (ONNX, one class: "failure")
and Obico's 1st-gen scoring: an exponentially weighted mean of the per-frame
detection confidence, compared against a long-running per-printer baseline.
The model is AGPL-3.0 (https://github.com/TheSpaghettiDetective/obico-server);
it is downloaded at first use, not shipped in this repo.
"""

import asyncio
import hashlib
import io
import json
import logging
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image

log = logging.getLogger("detect")

MODEL_URL = "https://www.obico.io/static_pub/ml-models/model-weights-5a6b1be1fa.onnx"
MODEL_SHA256 = "0a6ebd8e30dbf6a450c50f9c0a5406f04ba7eb1c99fd5996e888c78bb383b9aa"
MODEL_FILE = "obico-failure-5a6b1be1fa.onnx"

# Obico ml_api: a box counts at all above this confidence.
BOX_THRESH = 0.08
NMS_THRESH = 0.45
# Obico FD_1ST_GEN_PARAMS, tuned for one frame every 10 s.
EWM_SPAN = 12
ROLLING_WIN_SHORT = 310
ROLLING_WIN_LONG = 7200
THRESHOLD_LOW = 0.38
THRESHOLD_HIGH = 0.78
INIT_SAFE_FRAME_NUM = 30
ROLLING_MEAN_SHORT_MULTIPLE = 3.8
ESCALATING_FACTOR = 1.75
VISUALIZATION_THRESH = 0.2


class Model:
    """Lazily downloaded, verified ONNX model. One inference at a time."""

    def __init__(self, model_dir: Path, threads: int):
        self.path = model_dir / MODEL_FILE
        self.threads = threads
        self._session = None
        self._lock = asyncio.Lock()
        self.error = ""

    def _download(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        part = self.path.with_suffix(".part")
        log.info("downloading failure-detection model (~200 MB)")
        digest = hashlib.sha256()
        with urllib.request.urlopen(MODEL_URL, timeout=60) as resp, part.open("wb") as out:
            while chunk := resp.read(1 << 20):
                digest.update(chunk)
                out.write(chunk)
        if digest.hexdigest() != MODEL_SHA256:
            part.unlink(missing_ok=True)
            raise RuntimeError("model download failed its checksum")
        part.replace(self.path)
        log.info("model saved to %s", self.path)

    def _load(self):
        import onnxruntime as ort
        if not self.path.exists():
            self._download()
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = self.threads
        opts.inter_op_num_threads = 1
        return ort.InferenceSession(str(self.path), sess_options=opts,
                                    providers=["CPUExecutionProvider"])

    async def warm(self) -> None:
        """Load (and if needed download) the model now, so problems show at startup."""
        async with self._lock:
            if self._session is None:
                try:
                    self._session = await asyncio.to_thread(self._load)
                    self.error = ""
                    log.info("failure-detection model loaded")
                except Exception as e:
                    self.error = f"model unavailable: {e}"
                    log.error("%s", self.error)

    async def detect(self, jpeg: bytes) -> tuple[list[dict], tuple[int, int], float]:
        """Returns (boxes, (width, height), seconds). Boxes: {conf, x, y, w, h} in pixels."""
        async with self._lock:
            if self._session is None:
                try:
                    self._session = await asyncio.to_thread(self._load)
                    self.error = ""
                except Exception as e:
                    self.error = f"model unavailable: {e}"
                    raise
            start = time.monotonic()
            boxes, size = await asyncio.to_thread(self._infer, jpeg)
            return boxes, size, time.monotonic() - start

    def _infer(self, jpeg: bytes) -> tuple[list[dict], tuple[int, int]]:
        img = Image.open(io.BytesIO(jpeg)).convert("RGB")
        width, height = img.size
        inp = self._session.get_inputs()[0]
        in_h, in_w = inp.shape[2], inp.shape[3]
        arr = np.asarray(img.resize((in_w, in_h), Image.BILINEAR), dtype=np.float32) / 255.0
        arr = np.expand_dims(arr.transpose(2, 0, 1), 0)
        boxes, confs = self._session.run(None, {inp.name: arr})
        return postprocess(boxes, confs, width, height), (width, height)


def postprocess(boxes: np.ndarray, confs: np.ndarray, width: int, height: int) -> list[dict]:
    """YOLO output -> boxes above BOX_THRESH after NMS (single class)."""
    b = boxes[0, :, 0]          # [n, 4] x1 y1 x2 y2, 0..1
    c = confs[0].max(axis=1)    # [n]
    keep = c > BOX_THRESH
    b, c = b[keep], c[keep]
    out = []
    for i in nms(b, c, NMS_THRESH):
        x1, y1, x2, y2 = (float(v) for v in b[i])
        out.append({"conf": round(float(c[i]), 4),
                    "x": round(max(x1, 0) * width), "y": round(max(y1, 0) * height),
                    "w": round((min(x2, 1) - max(x1, 0)) * width),
                    "h": round((min(y2, 1) - max(y1, 0)) * height)})
    return sorted(out, key=lambda d: -d["conf"])


def nms(boxes: np.ndarray, confs: np.ndarray, thresh: float) -> list[int]:
    if not len(boxes):
        return []
    x1, y1, x2, y2 = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
    areas = (x2 - x1) * (y2 - y1)
    order = confs.argsort()[::-1]
    keep = []
    while order.size:
        i = order[0]
        keep.append(int(i))
        rest = order[1:]
        w = np.maximum(0.0, np.minimum(x2[i], x2[rest]) - np.maximum(x1[i], x1[rest]))
        h = np.maximum(0.0, np.minimum(y2[i], y2[rest]) - np.maximum(y1[i], y1[rest]))
        inter = w * h
        over = inter / (areas[i] + areas[rest] - inter + 1e-9)
        order = rest[over <= thresh]
    return keep


@dataclass
class Prediction:
    """Obico's per-printer prediction state."""
    ewm_mean: float = 0.0
    rolling_mean_short: float = 0.0
    rolling_mean_long: float = 0.0
    current_frame_num: int = 0
    lifetime_frame_num: int = 0
    current_p: float = 0.0

    def update(self, p: float) -> None:
        alpha = 2 / (EWM_SPAN + 1)
        self.current_p = p
        self.current_frame_num += 1
        self.lifetime_frame_num += 1
        self.ewm_mean = p * alpha + self.ewm_mean * (1 - alpha)
        self.rolling_mean_short = _rolling(p, self.rolling_mean_short,
                                           self.current_frame_num, ROLLING_WIN_SHORT)
        self.rolling_mean_long = _rolling(p, self.rolling_mean_long,
                                          self.lifetime_frame_num, ROLLING_WIN_LONG)

    def reset_print(self) -> None:
        """New print (or resumed after a pause): start the short-term stats over."""
        self.current_frame_num = 0
        self.ewm_mean = self.rolling_mean_long
        self.rolling_mean_short = self.rolling_mean_long
        self.current_p = 0.0

    def failing(self, sensitivity: float, escalating: float = 1.0) -> bool:
        if self.current_frame_num < INIT_SAFE_FRAME_NUM:
            return False
        adjusted = (self.ewm_mean - self.rolling_mean_long) * sensitivity / escalating
        if adjusted < THRESHOLD_LOW:
            return False
        if adjusted > THRESHOLD_HIGH:
            return True
        return adjusted > (self.rolling_mean_short - self.rolling_mean_long) \
            * ROLLING_MEAN_SHORT_MULTIPLE

    def normalized(self, sensitivity: float) -> float:
        """0..1 for display: <1/3 fine, 1/3..2/3 warning, >2/3 failing."""
        warn = (self.rolling_mean_short - self.rolling_mean_long) * ROLLING_MEAN_SHORT_MULTIPLE
        warn = min(THRESHOLD_HIGH, max(THRESHOLD_LOW, warn))
        fail = warn * ESCALATING_FACTOR
        p = (self.ewm_mean - self.rolling_mean_long) * sensitivity
        if p > fail:
            return _scale(p, fail, fail * 1.5, 2 / 3, 1.0)
        if p > warn:
            return _scale(p, warn, fail, 1 / 3, 2 / 3)
        return _scale(p, 0, warn, 0, 1 / 3)


def _rolling(p: float, mean: float, count: int, win: int) -> float:
    return mean + (p - mean) / float(win if win <= count else count + 1)


def _scale(v: float, lo: float, hi: float, new_lo: float, new_hi: float) -> float:
    return min(new_hi, max(new_lo, (v - lo) * (new_hi - new_lo) / (hi - lo) + new_lo))


class PredictionStore:
    """Persists each printer's long-term baseline across restarts."""

    def __init__(self, path: Path):
        self.path = path
        try:
            self.data = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            self.data = {}

    def get(self, printer: str) -> Prediction:
        raw = self.data.get(printer) or {}
        known = Prediction.__dataclass_fields__
        return Prediction(**{k: v for k, v in raw.items() if k in known})

    def put(self, printer: str, pred: Prediction) -> None:
        self.data[printer] = pred.__dict__.copy()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        tmp.replace(self.path)


@dataclass
class Snapshot:
    jpeg: bytes
    taken: float
    size: tuple[int, int] = (0, 0)
    boxes: list[dict] = field(default_factory=list)


def save_flagged(folder: Path, kind: str, snap: Snapshot, keep: int = 40) -> Path:
    """Keep a copy of the frame that triggered an alert or pause."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{datetime.fromtimestamp(snap.taken):%Y%m%d_%H%M%S}_{kind}.jpg"
    path.write_bytes(snap.jpeg)
    for old in sorted(folder.glob("*.jpg"))[:-keep]:
        old.unlink(missing_ok=True)
    return path
