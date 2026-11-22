"""Synthetic scenes for the moved-part detector (python -m unittest tests.test_motion)."""

import io
import unittest

import numpy as np
from PIL import Image

from printwarden import motion

W, H = 640, 360
STEP = 10          # seconds between snapshots


def scene(rng, parts, head=None):
    """Bed with noise, gray 'parts' (x, y, w, h) and a dark 'toolhead' box."""
    img = np.full((H, W), 120, dtype=np.float32)
    img[: H // 3] = 60                                   # back wall
    img += rng.normal(0, 4, img.shape)
    for x, y, w, h in parts:
        img[y:y + h, x:x + w] = 210
    if head is not None:
        x, y = head
        img[max(0, y - 120):y, max(0, x - 50):x + 50] = 25
    buf = io.BytesIO()
    Image.fromarray(np.clip(img, 0, 255).astype(np.uint8)).save(buf, "JPEG", quality=90)
    return buf.getvalue()


def head_px(pos):
    """Toolhead mm -> image px (bed 0..250 mm over the image width)."""
    return int(40 + pos[0] * 2.2), int(160 + pos[1] * 0.6)


def run(frames):
    """frames: list of (parts, head_pos_mm or None, pos_known). Returns states seen."""
    rng = np.random.default_rng(1)
    w = motion.MotionWatch()
    w.started = 0
    states = []
    for i, (parts, pos, known) in enumerate(frames):
        jpeg = scene(rng, parts, head_px(pos) if pos else None)
        r = w.update(jpeg, i * STEP, (W, H), 1.0, pos if known else None)
        states.append(r.state)
    return states


PARTS = [(150, 230, 60, 40), (300, 240, 60, 40), (450, 230, 60, 40)]
ROAM = [(20, 30), (90, 40), (160, 35), (60, 80), (130, 90), (200, 70)]


def roaming(n, parts=PARTS, known=True, start=0):
    return [(parts, ROAM[(start + i) % len(ROAM)], known) for i in range(n)]


class MotionTest(unittest.TestCase):
    def test_quiet_print(self):
        self.assertNotIn("moved", run(roaming(90)))

    def test_part_knocked_off(self):
        moved = [PARTS[0], PARTS[1], (470, 300, 40, 60)]    # third part tipped and slid
        states = run(roaming(40) + roaming(50, moved, start=40))
        self.assertIn("moved", states)
        self.assertLess(states.index("moved"), 40 + 30)      # within 5 min

    def test_part_knocked_off_without_positions(self):
        moved = [PARTS[0], PARTS[1], (470, 300, 40, 60)]
        states = run(roaming(40, known=False) + roaming(50, moved, known=False, start=40))
        self.assertIn("moved", states)

    def test_toolhead_parked_then_leaves(self):
        # Sits on one spot for 4 minutes (long purge / slow part), then roams again.
        parked = [(PARTS, (230, 10), True)] * 24
        states = run(roaming(40) + parked + roaming(40, start=40))
        self.assertNotIn("moved", states)

    def test_light_switched_on(self):
        rng = np.random.default_rng(2)
        w = motion.MotionWatch()
        w.started = 0
        states = []
        for i in range(90):
            img = Image.open(io.BytesIO(scene(rng, PARTS, head_px(ROAM[i % 6])))).convert("L")
            if i >= 40:   # much brighter and different contrast from here on
                img = img.point(lambda v: min(255, int(v * 0.5 + 140)))
            buf = io.BytesIO()
            img.save(buf, "JPEG")
            states.append(w.update(buf.getvalue(), i * STEP, (W, H), 1.0, ROAM[i % 6]).state)
        self.assertNotIn("moved", states)


if __name__ == "__main__":
    unittest.main()
