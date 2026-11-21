"""Detect a part that moved, tipped over or came off the bed.

The spaghetti model only knows what loose strands look like; a part that was
knocked loose still looks like a clean part. This compares the scene with how
it looked a couple of minutes earlier instead.

- Each snapshot is shrunk to a small grayscale image.
- "Now" is the pixel-wise median of the last WINDOW_S seconds, the reference
  the median of the WINDOW_S before that. The median removes the toolhead
  wherever it does not stay put; cells it kept passing over in either window
  are not steady (STABLE_RATIO) and are left out instead of guessed at.
- Cells are compared with block matching (each cell may sit +-SHIFT_X/SHIFT_Y
  px away), so the bed lowering, a delta effector rising and the thin band of
  new layers do not count. Brightness is matched first.
- Changed cells are grouped; groups smaller than MIN_CLUSTER are noise. A total
  over AREA_THRESH / sensitivity makes a *candidate*, with its reference frozen.
- A toolhead that sat in one spot for minutes looks exactly like a new object,
  so a candidate is only confirmed once the change is still there in frames
  where the toolhead (from Klipper) is AWAY_MM or more from where it was when
  the change appeared. A moved part stays changed; a parked toolhead does not.
  Without toolhead positions (or if it never leaves) the windows alone decide
  after MAX_WAIT_S.
- If most comparable cells change at once (light switched, camera bumped) the
  check is skipped until the windows have rolled past it.
"""

import io
import math
import time
from collections import deque
from dataclasses import dataclass, field

import numpy as np
from PIL import Image

WIDTH = 192            # analysis width in px (height follows the camera aspect)
CELL = 8               # px per grid cell
WINDOW_S = 120         # median window ("now" and the reference)
LAG_S = 120            # the reference window ends this long before "now" ends
MIN_FRAMES = 6         # frames a window needs to count
STABLE_RATIO = 0.6     # share of a window's frames that must agree with its median
SHIFT_X, SHIFT_Y = 3, 8   # px a cell may have moved and still match (bed lowering)
DIFF_THRESH = 22.0     # mean abs difference (0..255) for a cell to count as changed
MIN_CLUSTER = 3        # cells
AREA_THRESH = 0.012    # changed area (fraction of the image) at sensitivity 1
GLOBAL_CHANGE = 0.45   # more than this share changed = lighting / camera change
GRACE_S = 240          # no checks this early in a print (first layers)
CONFIRM_S = 90         # a candidate is watched at least this long
AWAY_MM = 40.0         # toolhead this far from where it was = it is not the change
AWAY_FRAMES = 4        # "toolhead away" frames needed to decide
AWAY_RATIO = 0.7       # of those, share that must still show the change
MAX_WAIT_S = 300       # give up waiting for the toolhead to leave; windows decide
WINDOW_RATIO = 0.7     # window-based confirm: share of comparable checks still changed
PARKED_MM = 20.0       # toolhead within this of its median = parked
PARKED_RATIO = 0.6     # ... for this share of a window's frames
KEEP_S = 600           # history kept (for a reference from before a long park)
JPEG_EVERY_S = 30      # keep a full snapshot this often, for the alert's "before" image


@dataclass
class Frame:
    t: float
    gray: np.ndarray                     # uint8, (h, w)
    pos: tuple[float, float] | None      # toolhead X, Y in mm


@dataclass
class MotionResult:
    score: float = 0.0         # changed area / threshold; >= 1 means over
    area: float = 0.0          # changed fraction of the image
    cells: list = field(default_factory=list)   # [(x, y, w, h)] in snapshot px
    state: str = "learning"    # learning | ok | candidate | moved
    note: str = ""


@dataclass
class WindowStats:
    med: np.ndarray            # float32 median image
    stable: np.ndarray         # bool cell grid: most frames agree with the median


def to_gray(jpeg: bytes) -> np.ndarray:
    img = Image.open(io.BytesIO(jpeg))
    img.draft("L", (WIDTH * 2, WIDTH * 2))
    img = img.convert("L")
    h = max(CELL, round(img.height * WIDTH / img.width / CELL) * CELL)
    return np.asarray(img.resize((WIDTH, h), Image.BILINEAR), dtype=np.uint8)


def cell_mean(a: np.ndarray) -> np.ndarray:
    gh, gw = a.shape[-2] // CELL, a.shape[-1] // CELL
    a = a[..., :gh * CELL, :gw * CELL]
    return a.reshape(*a.shape[:-2], gh, CELL, gw, CELL).mean(axis=(-3, -1))


def window_stats(frames: list[Frame]) -> WindowStats:
    """Median image of a window, and which cells were steady through it."""
    stack = np.stack([f.gray for f in frames]).astype(np.float32)
    med = np.median(stack, axis=0)
    agree = cell_mean(np.abs(stack - med)) < DIFF_THRESH
    return WindowStats(med, agree.mean(axis=0) >= STABLE_RATIO)


def block_diff(now: np.ndarray, ref: np.ndarray) -> np.ndarray:
    """Boolean cell grid where now and ref differ, tolerating small shifts."""
    now = now.astype(np.float32)
    m = ref.mean()
    if m > 1:
        ref = ref * (now.mean() / m)
    h, w = now.shape
    gh, gw = h // CELL, w // CELL
    now = now[:gh * CELL, :gw * CELL]
    p = max(SHIFT_X, SHIFT_Y)
    pad = np.pad(ref, p, mode="edge")
    best = np.full((gh, gw), np.inf, dtype=np.float32)
    for dy in range(-SHIFT_Y, SHIFT_Y + 1):
        for dx in range(-SHIFT_X, SHIFT_X + 1):
            d = np.abs(now - pad[p + dy:p + dy + gh * CELL, p + dx:p + dx + gw * CELL])
            np.minimum(best, cell_mean(d), out=best)
    return best > DIFF_THRESH


def clusters(grid: np.ndarray) -> list[list[tuple[int, int]]]:
    """4-connected groups of changed cells, at least MIN_CLUSTER big."""
    seen = np.zeros_like(grid, dtype=bool)
    out = []
    gh, gw = grid.shape
    for y, x in zip(*np.nonzero(grid)):
        if seen[y, x]:
            continue
        stack, group = [(y, x)], []
        seen[y, x] = True
        while stack:
            cy, cx = stack.pop()
            group.append((cy, cx))
            for ny, nx in ((cy - 1, cx), (cy + 1, cx), (cy, cx - 1), (cy, cx + 1)):
                if 0 <= ny < gh and 0 <= nx < gw and grid[ny, nx] and not seen[ny, nx]:
                    seen[ny, nx] = True
                    stack.append((ny, nx))
        if len(group) >= MIN_CLUSTER:
            out.append(group)
    return out


def median_pos(frames: list[Frame]) -> tuple[float, float] | None:
    pts = [f.pos for f in frames if f.pos is not None]
    if len(pts) < max(2, len(frames) // 2):
        return None
    return (float(np.median([p[0] for p in pts])), float(np.median([p[1] for p in pts])))


def parked_at(frames: list[Frame]) -> tuple[float, float] | None:
    """Where the toolhead sat for most of these frames, or None if it moved around."""
    mid = median_pos(frames)
    if mid is None:
        return None
    near = sum(1 for f in frames if f.pos is not None and math.dist(f.pos, mid) < PARKED_MM)
    return mid if near >= PARKED_RATIO * len(frames) else None


class MotionWatch:
    """Per-printer state for one print."""

    def __init__(self):
        self.frames: deque[Frame] = deque()
        self.jpegs: deque[tuple[float, bytes]] = deque()
        self.started = time.time()
        self.candidate: dict | None = None
        self.last = MotionResult()
        self.before_jpeg: bytes | None = None   # snapshot from before the change
        self.global_change = False

    def reset(self, keep_frames: bool = False, now: float | None = None) -> None:
        if now is None:
            now = self.frames[-1].t if self.frames else time.time()
        if not keep_frames:
            self.frames.clear()
            self.jpegs.clear()
        self.started = now
        self.candidate = None
        self.before_jpeg = None
        self.last = MotionResult()

    def window(self, end: float) -> list[Frame]:
        return [f for f in self.frames if end - WINDOW_S < f.t <= end]

    def update(self, jpeg: bytes, t: float, size: tuple[int, int], sensitivity: float = 1.0,
               pos: tuple[float, float] | None = None) -> MotionResult:
        frame = Frame(t, to_gray(jpeg), pos)
        if self.frames and self.frames[-1].gray.shape != frame.gray.shape:
            self.reset(now=t)        # camera resolution changed
        self.frames.append(frame)
        while self.frames and self.frames[0].t < t - KEEP_S:
            self.frames.popleft()
        if not self.jpegs or t - self.jpegs[-1][0] >= JPEG_EVERY_S:
            self.jpegs.append((t, jpeg))
        while self.jpegs and self.jpegs[0][0] < t - KEEP_S:
            self.jpegs.popleft()
        self.last = self._check(t, size, AREA_THRESH / max(0.1, sensitivity))
        return self.last

    def _check(self, t: float, size: tuple[int, int], thresh: float) -> MotionResult:
        now_frames = self.window(t)
        ref_frames = self.window(t - LAG_S)
        if self.candidate is None and (t - self.started < GRACE_S
                                       or len(now_frames) < MIN_FRAMES
                                       or len(ref_frames) < MIN_FRAMES):
            return MotionResult(state="learning")
        now = window_stats(now_frames)
        if self.candidate is not None:
            return self._confirm(t, now, size, thresh)

        ref = window_stats(ref_frames)
        valid = now.stable & ref.stable
        grid = block_diff(now.med, ref.med) & valid
        if grid.sum() > GLOBAL_CHANGE * max(1, valid.sum()):
            note = "" if self.global_change else \
                "Most of the picture changed (light or camera); waiting for it to settle"
            self.global_change = True
            return MotionResult(state="ok", note=note)
        self.global_change = False
        groups = clusters(grid)
        area = sum(len(g) for g in groups) / grid.size
        res = MotionResult(score=area / thresh, area=area, state="ok",
                           cells=self._boxes(groups, grid.shape, size))
        if res.score >= 1:
            cells = np.zeros_like(grid)
            for g in groups:
                for y, x in g:
                    cells[y, x] = True
            if self._toolhead_left(ref_frames, now, cells):
                return res
            self.candidate = {"since": t, "ref": ref, "cells": cells,
                              "pos": median_pos(now_frames),
                              "checks": 0, "hits": 0, "away": 0, "away_hits": 0}
            self.before_jpeg = self._jpeg_near(t - LAG_S - WINDOW_S / 2)
            res.state = "candidate"
        return res

    def _confirm(self, t: float, now: WindowStats, size: tuple[int, int],
                 thresh: float) -> MotionResult:
        c = self.candidate
        cells, ref = c["cells"], c["ref"]
        n_cells = int(cells.sum())
        diff = block_diff(now.med, ref.med)
        valid = now.stable & ref.stable
        if (diff & valid).sum() > GLOBAL_CHANGE * max(1, valid.sum()):
            self.candidate = None
            self.before_jpeg = None
            return MotionResult(state="ok", note="Picture changed as a whole; dropped the "
                                                 "possible move")
        # Window check: of the candidate cells we can see steadily, are most still changed?
        comparable = cells & valid
        if comparable.sum() >= 0.4 * n_cells:
            c["checks"] += 1
            if (diff & comparable).sum() >= 0.5 * comparable.sum():
                c["hits"] += 1
        # Toolhead check: with the toolhead well away, does the latest frame still differ?
        frame = self.frames[-1]
        if c["pos"] is not None and frame.pos is not None \
                and math.dist(c["pos"], frame.pos) >= AWAY_MM:
            c["away"] += 1
            if (block_diff(frame.gray, ref.med) & cells).sum() >= 0.5 * n_cells:
                c["away_hits"] += 1

        groups = clusters(diff & valid & self._near(cells))
        area = sum(len(g) for g in groups) / diff.size
        res = MotionResult(score=area / thresh, area=area, state="candidate",
                           cells=self._boxes(groups, diff.shape, size))
        waited = t - c["since"]
        if waited < CONFIRM_S:
            return res
        window_ok = c["checks"] >= 3 and c["hits"] >= WINDOW_RATIO * c["checks"]
        if c["away"] >= AWAY_FRAMES:
            moved = c["away_hits"] >= AWAY_RATIO * c["away"] and window_ok
        elif c["pos"] is not None and waited < MAX_WAIT_S:
            return res               # wait for the toolhead to go somewhere else
        else:
            moved = window_ok
        if moved:
            res.state = "moved"
            return res
        self.candidate = None
        self.before_jpeg = None
        res.state = "ok"
        res.note = "Change did not stay; it was the toolhead or something passing by"
        return res

    def _toolhead_left(self, ref_frames: list[Frame], now: WindowStats,
                       cells: np.ndarray) -> bool:
        """True if the toolhead was parked through the reference window and the scene now
        matches how it looked earlier with the toolhead somewhere else: it just left."""
        spot = parked_at(ref_frames)
        if spot is None:
            return False
        start = ref_frames[0].t
        older = [f for f in self.frames if f.t < start and f.pos is not None
                 and math.dist(f.pos, spot) >= AWAY_MM]
        if len(older) < MIN_FRAMES // 2:
            return True      # no clean "before" to compare with: do not risk an alarm
        before = np.median(np.stack([f.gray for f in older]).astype(np.float32), axis=0)
        still = (block_diff(now.med, before) & cells).sum()
        return still < 0.5 * cells.sum()

    @staticmethod
    def _near(cells: np.ndarray) -> np.ndarray:
        out = cells.copy()
        out[1:] |= cells[:-1]
        out[:-1] |= cells[1:]
        out[:, 1:] |= out[:, :-1].copy()
        out[:, :-1] |= out[:, 1:].copy()
        return out

    def _jpeg_near(self, t: float) -> bytes | None:
        if not self.jpegs:
            return None
        return min(self.jpegs, key=lambda j: abs(j[0] - t))[1]

    @staticmethod
    def _boxes(groups, shape, size) -> list[tuple[int, int, int, int]]:
        gh, gw = shape
        w, h = size if size and size[0] else (gw * CELL, gh * CELL)
        sx, sy = w / gw, h / gh
        out = []
        for g in groups:
            ys = [p[0] for p in g]
            xs = [p[1] for p in g]
            out.append((round(min(xs) * sx), round(min(ys) * sy),
                        round((max(xs) - min(xs) + 1) * sx), round((max(ys) - min(ys) + 1) * sy)))
        return out
