"""Build one YOLO training set from public datasets plus our own labelled frames.

    python -I tools/build_dataset.py OUT_DIR SOURCE.zip [SOURCE.zip ...]

Each SOURCE is a YOLO-format zip: a Roboflow "YOLOv8" export (train/ valid/
test/ with images/ + labels/, data.yaml naming the classes) or our own
/api/training/export.zip (images/ + labels/ + classes.txt, no split; those
frames are split 80/10/10 by name so a frame always lands in the same split).

Class names are mapped onto our label types (the order of LABELS in
timelapse_offload/training.py, which is also the class id order here).
Boxes with a class we have no type for are dropped. Roboflow keeps each
upload's original file name; these sets reuse one name for rotated or cropped
copies of the same scene, and share scenes between sets. All images with one
original name go to the split the first of them was in, so a scene cannot be
in both train and test, and copies that look the same (same bytes, or nearly
the same pixels) are skipped. Unreadable images are skipped. Zip member paths
are never used as output paths. ATTRIBUTION.txt lists every source and its
licence. Needs Pillow and NumPy.
"""

import hashlib
import io
import re
import sys
import zipfile
from collections import Counter
from pathlib import Path

import numpy as np
from PIL import Image

# Same order as timelapse_offload/training.py LABELS
CLASSES = ["spaghetti", "detached", "layer_shift", "warping", "stringing",
           "under_extrusion", "over_extrusion", "blob", "cracks", "other"]

# Source class names (lower-cased, spaces/hyphens as _) -> our type
ALIASES = {
    "spaghetti": "spaghetti", "stringing": "stringing", "stringging": "stringing",
    "warping": "warping", "under_extrusion": "under_extrusion", "over_extrusion": "over_extrusion",
    "blob": "blob", "blobs": "blob", "zits": "blob", "crack": "cracks", "cracks": "cracks",
    "layer_shift": "layer_shift", "detached": "detached", "other": "other",
}
SPLITS = {"train": "train", "valid": "valid", "val": "valid", "test": "test"}
IMG = re.compile(r"\.(jpe?g|png|bmp|webp)$", re.I)


def thumb(data: bytes):
    """Small grey copy for spotting repeats; None if the image does not decode."""
    try:
        with Image.open(io.BytesIO(data)) as im:
            return np.asarray(im.convert("L").resize((48, 48)), dtype=np.float32)
    except Exception:
        return None


def norm(name: str) -> str:
    return re.sub(r"[\s-]+", "_", name.strip().lower())


def class_names(z: zipfile.ZipFile) -> list[str]:
    names = set(z.namelist())
    if "data.yaml" in names:
        text = z.read("data.yaml").decode("utf-8", "replace")
        m = re.search(r"^names:\s*\[(.*?)\]", text, re.M | re.S)
        if m:
            return [s.strip().strip("'\"") for s in m.group(1).split(",")]
        # block list form:  names:\n  - a\n  - b   or   0: a
        block = text.split("names:", 1)[1]
        out = []
        for line in block.splitlines()[1:]:
            mm = re.match(r"\s+(?:-|\d+:)\s*(.+)", line)
            if not mm:
                break
            out.append(mm.group(1).strip().strip("'\""))
        return out
    if "classes.txt" in names:
        return [l.strip() for l in z.read("classes.txt").decode().splitlines() if l.strip()]
    raise SystemExit("no data.yaml or classes.txt in the zip")


def source_info(z: zipfile.ZipFile, path: Path) -> tuple[str, str]:
    names = set(z.namelist())
    if "data.yaml" in names:
        text = z.read("data.yaml").decode("utf-8", "replace")
        url = re.search(r"url:\s*(\S+)", text)
        lic = re.search(r"license:\s*(.+)", text)
        return (url.group(1) if url else path.name), (lic.group(1).strip() if lic else "unknown")
    return path.name, "own frames (TimelapseOffload label page)"


def main() -> None:
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    out = Path(sys.argv[1])
    for split in ("train", "valid", "test"):
        (out / split / "images").mkdir(parents=True, exist_ok=True)
        (out / split / "labels").mkdir(parents=True, exist_ok=True)
    seen_hash = set()
    scenes = {}   # original name -> (split, [thumbs])
    attribution, totals = [], Counter()

    for n, src in enumerate(sys.argv[2:]):
        path = Path(src)
        z = zipfile.ZipFile(path)
        names = class_names(z)
        mapping = {i: ALIASES.get(norm(c)) for i, c in enumerate(names)}
        url, lic = source_info(z, path)
        tag = re.sub(r"[^a-z0-9]+", "", path.stem.split(".")[0].lower())[:12] or f"src{n}"
        dropped = sorted({names[i] for i, v in mapping.items() if v is None})
        stats = Counter()
        members = set(z.namelist())
        for m in z.namelist():
            parts = m.split("/")
            if not IMG.search(m) or "images" not in parts:
                continue
            split = SPLITS.get(parts[0])
            if split is None:   # our export: no split, decide by name
                split = ("valid", "test", *["train"] * 8)[int(hashlib.md5(m.encode()).hexdigest(), 16) % 10]
            data = z.read(m)
            digest = hashlib.md5(data).hexdigest()
            if digest in seen_hash:
                stats["duplicate"] += 1
                continue
            small = thumb(data)
            if small is None:
                stats["unreadable"] += 1
                continue
            seen_hash.add(digest)
            if ".rf." in parts[-1]:
                orig = re.sub(r"\.rf\.[0-9a-f]+$", "", Path(parts[-1]).stem).lower()
                if orig in scenes:
                    first_split, thumbs = scenes[orig]
                    if any(float(np.abs(small - t).mean()) < 3 for t in thumbs):
                        stats["duplicate"] += 1
                        continue
                    if split != first_split:
                        stats[f"moved to {first_split} (same scene)"] += 1
                    split = first_split
                    thumbs.append(small)
                else:
                    scenes[orig] = (split, [small])
            lab = "/".join(parts[:-2] + ["labels", Path(parts[-1]).stem + ".txt"])
            lines = []
            if lab in members:
                for line in z.read(lab).decode("utf-8", "replace").splitlines():
                    f = line.split()
                    if len(f) != 5:
                        continue   # polygons or junk: skip the box
                    ours = mapping.get(int(f[0])) if f[0].isdigit() else None
                    if ours is None:
                        stats["box dropped"] += 1
                        continue
                    try:
                        vals = [min(1.0, max(0.0, float(v))) for v in f[1:]]
                    except ValueError:
                        continue
                    lines.append(f"{CLASSES.index(ours)} " + " ".join(f"{v:.6f}" for v in vals))
                    stats[ours] += 1
            name = f"{tag}_{digest[:12]}"
            ext = IMG.search(m).group(0).lower().replace("jpeg", "jpg")
            (out / split / "images" / (name + ext)).write_bytes(data)
            (out / split / "labels" / (name + ".txt")).write_text("\n".join(lines) + ("\n" if lines else ""))
            stats[f"images {split}"] += 1
        totals.update(stats)
        attribution.append(f"{path.name}\n  source: {url}\n  licence: {lic}\n  classes: {names}\n"
                           f"  not used (no matching type): {dropped or 'none'}\n  imported: {dict(stats)}\n")
        print(path.name, dict(stats))

    (out / "data.yaml").write_text(
        f"path: {out.resolve().as_posix()}\ntrain: train/images\nval: valid/images\ntest: test/images\n"
        f"nc: {len(CLASSES)}\nnames: {CLASSES}\n")
    (out / "ATTRIBUTION.txt").write_text(
        "Training data for TimelapseOffload failure detection.\n"
        "Public sets are used under their licences (CC BY 4.0: credit the authors below).\n\n"
        + "\n".join(attribution))
    print("total", dict(totals))


if __name__ == "__main__":
    main()
