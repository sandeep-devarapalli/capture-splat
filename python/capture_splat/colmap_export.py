from __future__ import annotations

import argparse
import hashlib
import math
import shutil
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from .capture_schema import frame_selection_summary, iter_frames, load_capture
from .json_utils import write_json_strict


ARKIT_TO_OPENCV_CAMERA = np.diag([1.0, -1.0, -1.0, 1.0])


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _file_evidence(path: Path) -> dict[str, Any]:
    return {"bytes": path.stat().st_size, "checksum": _sha256(path)}


def rotation_matrix_to_quaternion_wxyz(matrix: np.ndarray) -> tuple[float, float, float, float]:
    m = matrix
    trace = float(np.trace(m))
    if trace > 0.0:
        s = math.sqrt(trace + 1.0) * 2.0
        quaternion = (0.25 * s, (m[2, 1] - m[1, 2]) / s, (m[0, 2] - m[2, 0]) / s, (m[1, 0] - m[0, 1]) / s)
    elif m[0, 0] > m[1, 1] and m[0, 0] > m[2, 2]:
        s = math.sqrt(1.0 + m[0, 0] - m[1, 1] - m[2, 2]) * 2.0
        quaternion = ((m[2, 1] - m[1, 2]) / s, 0.25 * s, (m[0, 1] + m[1, 0]) / s, (m[0, 2] + m[2, 0]) / s)
    elif m[1, 1] > m[2, 2]:
        s = math.sqrt(1.0 + m[1, 1] - m[0, 0] - m[2, 2]) * 2.0
        quaternion = ((m[0, 2] - m[2, 0]) / s, (m[0, 1] + m[1, 0]) / s, 0.25 * s, (m[1, 2] + m[2, 1]) / s)
    else:
        s = math.sqrt(1.0 + m[2, 2] - m[0, 0] - m[1, 1]) * 2.0
        quaternion = ((m[1, 0] - m[0, 1]) / s, (m[0, 2] + m[2, 0]) / s, (m[1, 2] + m[2, 1]) / s, 0.25 * s)
    norm = math.sqrt(sum(value * value for value in quaternion))
    if not math.isfinite(norm) or norm <= 0:
        raise ValueError("camera rotation produced an invalid quaternion")
    normalized = tuple(value / norm for value in quaternion)
    if normalized[0] < 0:
        normalized = tuple(-value for value in normalized)
    return normalized


def arkit_camera_to_colmap_pose(camera_to_world: np.ndarray) -> tuple[tuple[float, float, float, float], np.ndarray]:
    if camera_to_world.shape != (4, 4) or not np.all(np.isfinite(camera_to_world)):
        raise ValueError("ARKit camera_to_world must be a finite 4x4 matrix")
    if not np.allclose(camera_to_world[3], [0.0, 0.0, 0.0, 1.0], atol=1e-5, rtol=0.0):
        raise ValueError("ARKit camera_to_world must be affine")
    rotation = camera_to_world[:3, :3]
    if not np.allclose(rotation.T @ rotation, np.eye(3), atol=1e-5, rtol=0.0):
        raise ValueError("ARKit camera_to_world rotation must be orthonormal")
    determinant = float(np.linalg.det(rotation))
    if not math.isclose(determinant, 1.0, abs_tol=1e-5, rel_tol=0.0):
        raise ValueError("ARKit camera_to_world rotation must be right-handed")
    opencv_camera_to_world = camera_to_world @ ARKIT_TO_OPENCV_CAMERA
    world_to_camera_rotation = opencv_camera_to_world[:3, :3].T
    left, _, right = np.linalg.svd(world_to_camera_rotation)
    world_to_camera_rotation = left @ right
    if np.linalg.det(world_to_camera_rotation) < 0:
        left[:, -1] *= -1
        world_to_camera_rotation = left @ right
    translation = -world_to_camera_rotation @ camera_to_world[:3, 3]
    quaternion = rotation_matrix_to_quaternion_wxyz(world_to_camera_rotation)
    return quaternion, translation


def intrinsics_for_image(intrinsics: dict[str, float], image_path: Path) -> dict[str, float]:
    with Image.open(image_path) as image:
        width, height = image.size
    intr_w = int(intrinsics["w"])
    intr_h = int(intrinsics["h"])
    if intr_w <= 0 or intr_h <= 0:
        raise ValueError("intrinsics w/h must be positive")
    x_scale = width / intr_w
    y_scale = height / intr_h
    return {
        "fl_x": intrinsics["fl_x"] * x_scale,
        "fl_y": intrinsics["fl_y"] * y_scale,
        "cx": intrinsics["cx"] * x_scale,
        "cy": intrinsics["cy"] * y_scale,
        "w": float(width),
        "h": float(height),
    }


def export_colmap_text(capture_dir: Path, out_dir: Path, copy_images: bool = True) -> dict[str, Any]:
    capture_dir = capture_dir.resolve()
    out_dir = out_dir.resolve()
    data = load_capture(capture_dir)
    image_dir = out_dir / "images"
    sparse_dir = out_dir / "sparse" / "0"
    image_dir.mkdir(parents=True, exist_ok=True)
    sparse_dir.mkdir(parents=True, exist_ok=True)
    selection = frame_selection_summary(data)
    camera_ids: dict[tuple[float, float, float, float, int, int], int] = {}
    camera_lines: list[str] = ["# Camera list with one line of data per camera:", "# CAMERA_ID, MODEL, WIDTH, HEIGHT, PARAMS[]"]
    image_lines: list[str] = ["# Image list with two lines of data per image:", "# IMAGE_ID, QW, QX, QY, QZ, TX, TY, TZ, CAMERA_ID, NAME", "# POINTS2D[] as (X, Y, POINT3D_ID)"]
    image_count = 0
    for frame in iter_frames(data, accepted_only=True):
        src = (capture_dir / frame.image_path).resolve()
        if not src.exists():
            raise FileNotFoundError(f"frame image missing: {src}")
        intr = intrinsics_for_image(frame.intrinsics, src)
        key = (intr["fl_x"], intr["fl_y"], intr["cx"], intr["cy"], int(intr["w"]), int(intr["h"]))
        if key not in camera_ids:
            camera_id = len(camera_ids) + 1
            camera_ids[key] = camera_id
            camera_lines.append(f"{camera_id} PINHOLE {int(intr['w'])} {int(intr['h'])} {intr['fl_x']:.12g} {intr['fl_y']:.12g} {intr['cx']:.12g} {intr['cy']:.12g}")
        else:
            camera_id = camera_ids[key]
        name = f"{frame.index:06d}{src.suffix.lower() or '.jpg'}"
        if copy_images:
            shutil.copy2(src, image_dir / name)
        c2w = np.asarray(frame.transform_matrix, dtype=float)
        (qw, qx, qy, qz), translation = arkit_camera_to_colmap_pose(c2w)
        tx, ty, tz = translation
        image_lines.append(f"{frame.index} {qw:.12g} {qx:.12g} {qy:.12g} {qz:.12g} {tx:.12g} {ty:.12g} {tz:.12g} {camera_id} {name}")
        image_lines.append("")
        image_count += 1
    if image_count == 0:
        raise ValueError("capture has no accepted frames for COLMAP export")
    (sparse_dir / "cameras.txt").write_text("\n".join(camera_lines) + "\n", encoding="utf-8")
    (sparse_dir / "images.txt").write_text("\n".join(image_lines) + "\n", encoding="utf-8")
    (sparse_dir / "points3D.txt").write_text("# Empty seed cloud. Run COLMAP mapper/point_triangulator to add observations.\n", encoding="utf-8")
    cameras_path = sparse_dir / "cameras.txt"
    images_path = sparse_dir / "images.txt"
    points_path = sparse_dir / "points3D.txt"
    summary = {
        "schema": "capture_splat.colmap_export_summary.v0.1",
        "capture_dir": str(capture_dir),
        "output_dir": str(out_dir),
        "image_count": image_count,
        "camera_count": len(camera_ids),
        "sparse_dir": str(sparse_dir),
        "copied_images": copy_images,
        "frame_selection": selection,
        "coordinate_contract": {
            "source": "arkit_camera_to_world_x_right_y_up_z_back",
            "target": "colmap_world_to_camera_x_right_y_down_z_forward",
            "camera_to_world_conversion": "opencv_c2w = arkit_c2w @ diag(1,-1,-1,1)",
        },
        "inputs": {"capture_manifest": _file_evidence(capture_dir / "capture.json")},
        "outputs": {
            "cameras": _file_evidence(cameras_path),
            "images": _file_evidence(images_path),
            "points3D": _file_evidence(points_path),
        },
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
