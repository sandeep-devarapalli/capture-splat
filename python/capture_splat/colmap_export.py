from __future__ import annotations

import argparse
import math
import shutil
from pathlib import Path
from typing import Any

import numpy as np

from .capture_schema import iter_frames, load_capture
from .json_utils import write_json_strict


def rotation_matrix_to_quaternion_wxyz(matrix: np.ndarray) -> tuple[float, float, float, float]:
    m = matrix
    trace = float(np.trace(m))
    if trace > 0.0:
        s = math.sqrt(trace + 1.0) * 2.0
        return (0.25 * s, (m[2, 1] - m[1, 2]) / s, (m[0, 2] - m[2, 0]) / s, (m[1, 0] - m[0, 1]) / s)
    if m[0, 0] > m[1, 1] and m[0, 0] > m[2, 2]:
        s = math.sqrt(1.0 + m[0, 0] - m[1, 1] - m[2, 2]) * 2.0
        return ((m[2, 1] - m[1, 2]) / s, 0.25 * s, (m[0, 1] + m[1, 0]) / s, (m[0, 2] + m[2, 0]) / s)
    if m[1, 1] > m[2, 2]:
        s = math.sqrt(1.0 + m[1, 1] - m[0, 0] - m[2, 2]) * 2.0
        return ((m[0, 2] - m[2, 0]) / s, (m[0, 1] + m[1, 0]) / s, 0.25 * s, (m[1, 2] + m[2, 1]) / s)
    s = math.sqrt(1.0 + m[2, 2] - m[0, 0] - m[1, 1]) * 2.0
    return ((m[1, 0] - m[0, 1]) / s, (m[0, 2] + m[2, 0]) / s, (m[1, 2] + m[2, 1]) / s, 0.25 * s)


def export_colmap_text(capture_dir: Path, out_dir: Path, copy_images: bool = True) -> dict[str, Any]:
    capture_dir = capture_dir.resolve()
    out_dir = out_dir.resolve()
    data = load_capture(capture_dir)
    image_dir = out_dir / "images"
    sparse_dir = out_dir / "sparse" / "0"
    image_dir.mkdir(parents=True, exist_ok=True)
    sparse_dir.mkdir(parents=True, exist_ok=True)
    camera_ids: dict[tuple[float, float, float, float, int, int], int] = {}
    camera_lines: list[str] = ["# Camera list with one line of data per camera:", "# CAMERA_ID, MODEL, WIDTH, HEIGHT, PARAMS[]"]
    image_lines: list[str] = ["# Image list with two lines of data per image:", "# IMAGE_ID, QW, QX, QY, QZ, TX, TY, TZ, CAMERA_ID, NAME", "# POINTS2D[] as (X, Y, POINT3D_ID)"]
    image_count = 0
    for frame in iter_frames(data):
        intr = frame.intrinsics
        key = (intr["fl_x"], intr["fl_y"], intr["cx"], intr["cy"], int(intr["w"]), int(intr["h"]))
        if key not in camera_ids:
            camera_id = len(camera_ids) + 1
            camera_ids[key] = camera_id
            camera_lines.append(f"{camera_id} PINHOLE {int(intr['w'])} {int(intr['h'])} {intr['fl_x']:.12g} {intr['fl_y']:.12g} {intr['cx']:.12g} {intr['cy']:.12g}")
        else:
            camera_id = camera_ids[key]
        src = (capture_dir / frame.image_path).resolve()
        if not src.exists():
            raise FileNotFoundError(f"frame image missing: {src}")
        name = f"{frame.index:06d}{src.suffix.lower() or '.jpg'}"
        if copy_images:
            shutil.copy2(src, image_dir / name)
        c2w = np.asarray(frame.transform_matrix, dtype=float)
        w2c = np.linalg.inv(c2w)
        qw, qx, qy, qz = rotation_matrix_to_quaternion_wxyz(w2c[:3, :3])
        tx, ty, tz = w2c[:3, 3]
        image_lines.append(f"{frame.index} {qw:.12g} {qx:.12g} {qy:.12g} {qz:.12g} {tx:.12g} {ty:.12g} {tz:.12g} {camera_id} {name}")
        image_lines.append("")
        image_count += 1
    (sparse_dir / "cameras.txt").write_text("\n".join(camera_lines) + "\n", encoding="utf-8")
    (sparse_dir / "images.txt").write_text("\n".join(image_lines) + "\n", encoding="utf-8")
    (sparse_dir / "points3D.txt").write_text("# Empty seed cloud. Run COLMAP mapper/point_triangulator to add observations.\n", encoding="utf-8")
    summary = {
        "schema": "capture_splat.colmap_export_summary.v0.1",
        "capture_dir": str(capture_dir),
        "output_dir": str(out_dir),
        "image_count": image_count,
        "camera_count": len(camera_ids),
        "sparse_dir": str(sparse_dir),
        "copied_images": copy_images,
    }
    write_json_strict(out_dir / "capture_splat_colmap_summary.json", summary)
    return summary


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export Capture Splat capture poses as a COLMAP text package.")
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--no-copy-images", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    summary = export_colmap_text(args.capture, args.out, copy_images=not args.no_copy_images)
    print(summary["sparse_dir"])


if __name__ == "__main__":
    main()
