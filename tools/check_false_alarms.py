"""Run a trained detector over our own timelapse videos (good prints) and count
what it would have flagged. Everything found there is a false alarm.

    python tools/check_false_alarms.py WEIGHTS VIDEO_OR_DIR [...] [--frames 150] [--out DIR]

Samples evenly spaced frames from each video, runs the model at a low
confidence, and reports, for a few confidence thresholds, the share of frames
that would have shown a box (any type and per type). Writes a contact sheet of
the most confident detections to OUT/false_alarms.jpg so they can be looked at.
"""

import argparse
from collections import defaultdict
from pathlib import Path

THRESHOLDS = (0.25, 0.4, 0.5, 0.6, 0.7)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("weights")
    ap.add_argument("videos", nargs="+")
    ap.add_argument("--frames", type=int, default=150)
    ap.add_argument("--out", default=".")
    ap.add_argument("--imgsz", type=int, default=640)
    args = ap.parse_args()

    import cv2
    import numpy as np
    from ultralytics import YOLO

    model = YOLO(args.weights)
    files = []
    for v in args.videos:
        p = Path(v)
        files += sorted(p.glob("*.mp4")) if p.is_dir() else [p]

    hits = []          # (conf, video, frame index, image, boxes)
    per_video = {}
    for f in files:
        cap = cv2.VideoCapture(str(f))
        n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
        idx = np.linspace(0, n - 1, min(args.frames, n)).astype(int)
        best = []      # per sampled frame: {class: max conf}
        for i in idx:
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(i))
            ok, img = cap.read()
            if not ok:
                continue
            r = model.predict(img, imgsz=args.imgsz, conf=0.1, verbose=False)[0]
            per = defaultdict(float)
            boxes = []
            for b, c, k in zip(r.boxes.xyxy.tolist(), r.boxes.conf.tolist(), r.boxes.cls.tolist()):
                name = model.names[int(k)]
                per[name] = max(per[name], c)
                boxes.append((b, c, name))
            best.append(per)
            if boxes:
                hits.append((max(c for _, c, _ in boxes), f.name, int(i), img, boxes))
        cap.release()
        per_video[f.name] = best
        top = max((max(p.values()) for p in best if p), default=0)
        print(f"{f.name}: {len(best)} frames, highest confidence {top:.2f}")

    print("\nShare of frames with a box (false alarms), by confidence threshold:")
    allframes = [p for b in per_video.values() for p in b]
    print("  threshold  " + "  ".join(f"{t:>5}" for t in THRESHOLDS))
    print("  any type   " + "  ".join(f"{sum(1 for p in allframes if p and max(p.values()) >= t) / max(1, len(allframes)):5.1%}" for t in THRESHOLDS))
    names = sorted({k for p in allframes for k in p})
    for k in names:
        print(f"  {k[:9]:9s}  " + "  ".join(f"{sum(1 for p in allframes if p.get(k, 0) >= t) / max(1, len(allframes)):5.1%}" for t in THRESHOLDS))
    print("\n  per video at 0.5:")
    for v, b in per_video.items():
        print(f"    {v:24s} {sum(1 for p in b if p and max(p.values()) >= 0.5) / max(1, len(b)):5.1%}")

    # Contact sheet of the 16 most confident detections
    hits.sort(key=lambda h: -h[0])
    tiles = []
    for conf, vname, i, img, boxes in hits[:16]:
        im = img.copy()
        for (x1, y1, x2, y2), c, name in boxes:
            if c < 0.25:
                continue
            cv2.rectangle(im, (int(x1), int(y1)), (int(x2), int(y2)), (40, 40, 255), max(2, im.shape[1] // 300))
            cv2.putText(im, f"{name} {c:.2f}", (int(x1), max(18, int(y1) - 6)), cv2.FONT_HERSHEY_SIMPLEX,
                        im.shape[1] / 1100, (40, 40, 255), max(1, im.shape[1] // 500))
        im = cv2.resize(im, (480, int(480 * im.shape[0] / im.shape[1])))
        cv2.putText(im, f"{vname} #{i}", (6, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
        tiles.append(cv2.copyMakeBorder(im, 0, 360 - im.shape[0] if im.shape[0] < 360 else 0, 0, 0, cv2.BORDER_CONSTANT))
    if tiles:
        tiles = [t[:360] for t in tiles]
        while len(tiles) % 4:
            tiles.append(np.zeros_like(tiles[0]))
        sheet = np.vstack([np.hstack(tiles[r:r + 4]) for r in range(0, len(tiles), 4)])
        out = Path(args.out) / "false_alarms.jpg"
        cv2.imwrite(str(out), sheet, [cv2.IMWRITE_JPEG_QUALITY, 85])
        print("\ncontact sheet:", out)


if __name__ == "__main__":
    main()
