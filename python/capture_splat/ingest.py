from __future__ import annotations

import argparse
import shutil
from pathlib import Path
from typing import Any

from .capture_schema import CAPTURE_SCHEMA, iter_frames, load_capture
from .json_utils import write_json_strict

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".tif", ".tiff"}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Normalize a Capture Splat export into a Nerfstudio-style image package.")
    parser.add_argument("--capture", type=Path, required=True, help="Capture folder containing capture.json")
    parser.add_argument("--out", type=Path, required=True, help="Output directory")
    parser.add_argument("--no-copy-images", action="store_true", help="Reference source images instead of copying them")
    return parser.parse_args(argv)


def _copy_image(src: Path, dst_dir: Path, index: int) -> str:
    suffix = src.suffix.lower() if src.suffix.lower() in IMAGE_SUFFIXES else ".jpg"
    name = f"{index:06d}{suffix}"
    dst = dst_dir / name
    shutil.copy2(src, dst)
    return f"images/{name}"


def ingest_capture(capture_dir: Path, out_dir: Path, copy_images: bool = True) -> dict[str, Any]:
    capture_dir = capture_dir.resolve()
    out_dir = out_dir.resolve()
    data = load_capture(capture_dir)
    dataset_dir = out_dir / "nerfstudio_dataset"
    images_dir = dataset_dir / "images"
    if copy_images:
        images_dir.mkdir(parents=True, exist_ok=True)
    frames_payload: list[dict[str, Any]] = []
    first_intrinsics: dict[str, float] | None = None
    for frame in iter_frames(data):
        src = (capture_dir / frame.image_path).resolve()
        if not src.exists():
            raise FileNotFoundError(f"frame image missing: {src}")
        first_intrinsics = first_intrinsics or frame.intrinsics
        file_path = _copy_image(src, images_dir, frame.index) if copy_images else str(src)
        frames_payload.append({
            "file_path": file_path,
            "transform_matrix": frame.transform_matrix,
            "timestamp": frame.timestamp,
        })
    assert first_intrinsics is not None
    transforms = {
        "camera_model": "OPENCV",
        "fl_x": first_intrinsics["fl_x"],
        "fl_y": first_intrinsics["fl_y"],
        "cx": first_intrinsics["cx"],
        "cy": first_intrinsics["cy"],
        "w": int(first_intrinsics["w"]),
        "h": int(first_intrinsics["h"]),
        "frames": frames_payload,
    }
    write_json_strict(dataset_dir / "transforms.json", transforms)
    summary = {
        "schema": "capture_splat.ingest_summary.v0.1",
        "source_schema": data.get("schema"),
        "public_schema": CAPTURE_SCHEMA,
        "capture_dir": str(capture_dir),
        "output_dir": str(out_dir),
        "nerfstudio_dataset": str(dataset_dir),
        "frame_count": len(frames_payload),
        "copied_images": copy_images,
    }
    write_json_strict(out_dir / "capture_splat_ingest_summary.json", summary)
    return summary


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    summary = ingest_capture(args.capture, args.out, copy_images=not args.no_copy_images)
    print(summary["nerfstudio_dataset"])


if __name__ == "__main__":
    main()
