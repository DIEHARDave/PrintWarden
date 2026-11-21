"""Train the failure detector on a dataset made by build_dataset.py.

    python tools/train_detector.py DATASET_DIR [--model yolo11n.pt] [--epochs 80]
                                   [--imgsz 640] [--batch 16] [--name run]

Needs ultralytics and a CUDA build of PyTorch (falls back to CPU, very slowly).
Trains, scores the held-back test split, and exports the best weights to ONNX
for the server (onnxruntime is already in its image). Results land in
DATASET_DIR/../runs/<name>/: weights/best.pt, best.onnx, results.csv, plots,
and test_metrics.txt with mAP per problem type.
"""

import argparse
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("dataset")
    ap.add_argument("--model", default="yolo11n.pt", help="starting weights (pretrained on COCO)")
    ap.add_argument("--epochs", type=int, default=80)
    ap.add_argument("--imgsz", type=int, default=640)
    ap.add_argument("--batch", type=int, default=16)
    ap.add_argument("--name", default="run")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    import torch
    from ultralytics import YOLO

    data = Path(args.dataset).resolve() / "data.yaml"
    project = Path(args.dataset).resolve().parent / "runs"
    device = 0 if torch.cuda.is_available() else "cpu"
    print("device:", torch.cuda.get_device_name(0) if device == 0 else "CPU")

    model = YOLO(args.model)
    model.train(
        data=str(data), epochs=args.epochs, imgsz=args.imgsz, batch=args.batch, device=device,
        workers=args.workers, project=str(project), name=args.name, exist_ok=True,
        patience=20, optimizer="AdamW", lr0=0.002, cos_lr=True, close_mosaic=10,
        # The public images are mostly upright photos; mild geometry changes only
        degrees=5, translate=0.08, scale=0.4, shear=1.0, fliplr=0.5, mosaic=0.8, mixup=0.05,
        hsv_h=0.015, hsv_s=0.5, hsv_v=0.4,
        plots=True, verbose=False,
    )

    run = project / args.name
    best = YOLO(str(run / "weights" / "best.pt"))
    res = best.val(data=str(data), split="test", imgsz=args.imgsz, batch=args.batch, device=device,
                   project=str(project), name=args.name + "_test", exist_ok=True, plots=True, verbose=False)
    lines = [f"test split: mAP50 {res.box.map50:.3f}  mAP50-95 {res.box.map:.3f}  "
             f"precision {res.box.mp:.3f}  recall {res.box.mr:.3f}"]
    for i, c in enumerate(res.box.ap_class_index):
        lines.append(f"  {best.names[int(c)]:16s} mAP50 {res.box.ap50[i]:.3f}  mAP50-95 {res.box.ap[i]:.3f}")
    (run / "test_metrics.txt").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))

    onnx = best.export(format="onnx", imgsz=args.imgsz, opset=12, simplify=True, dynamic=False)
    print("ONNX:", onnx)


if __name__ == "__main__":
    main()
