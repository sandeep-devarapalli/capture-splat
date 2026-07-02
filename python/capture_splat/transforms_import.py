from __future__ import annotations

import argparse
import shutil
from pathlib import Path
from typing import Any

from PIL import Image

from .capture_schema import CAPTURE_SCHEMA, load_capture
from .json_utils import ensure_finite, load_json_strict, write_json_strict

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".tif", ".tiff"}
DEPTH_SUFFIXES = {".exr", ".npy", ".png", ".tif", ".tiff"}
DEPTH_KEYS = ("depth_file_path", "depth_path", "depth", "depth_file")


def _resolve(root: Path, value: Any) -> Path | None:
    if not isinstance(value, str) or not value:
        return None
    path = Path(value)
    return path if path.is_absolute() else root / path


def _copy(src: Path, dst_dir: Path, index: int, allowed_suffixes: set[str], fallback_suffix: str) -> str:
    if not src.exists():
        raise FileNotFoundError(f"source file missing: {src}")
    suffix = src.suffix.lower() if src.suffix.lower() in allowed_suffixes else fallback_suffix
    name = f"{index:06d}{suffix}"
    dst = dst_dir / name
    shutil.copy2(src, dst)
    return f"{dst_dir.name}/{name}"


def _matrix(frame: dict[str, Any]) -> list[list[float]]:
    matrix = frame.get("transform_matrix") or frame.get("camera_to_world")
    if not isinstance(matrix, list) or len(matrix) != 4:
        raise ValueError("frame transform_matrix must be a 4x4 list")
    rows = []
    for row in matrix:
        if not isinstance(row, list) or len(row) != 4:
            raise ValueError("frame transform_matrix must be a 4x4 list")
        rows.append([float(value) for value in row])
    return rows


def _image_size(path: Path) -> tuple[int, int]:
    with Image.open(path) as image:
        return image.size


def _intrinsics(transforms: dict[str, Any], frame: dict[str, Any], image_path: Path) -> dict[str, float]:
    width, height = _image_size(image_path)
    values = {key: frame.get(key, transforms.get(key)) for key in ("fl_x", "fl_y", "cx", "cy", "w", "h")}
    values["w"] = values["w"] if values["w"] is not None else width
    values["h"] = values["h"] if values["h"] is not None else height
    missing = [key for key, value in values.items() if value is None]
    if missing:
        raise ValueError(f"intrinsics missing keys: {missing}")
    return {
        "fl_x": float(values["fl_x"]),
        "fl_y": float(values["fl_y"]),
        "cx": float(values["cx"]),
        "cy": float(values["cy"]),
        "w": int(values["w"]),
        "h": int(values["h"]),
    }


def _depth_path(root: Path, frame: dict[str, Any]) -> Path | None:
    for key in DEPTH_KEYS:
        path = _resolve(root, frame.get(key))
        if path is not None:
            return path
    return None


def import_transforms_package(
    source_dir: Path,
    out_dir: Path,
    copy_files: bool = True,
    require_depth: bool = False,
) -> dict[str, Any]:
    source_dir = source_dir.resolve()
    out_dir = out_dir.resolve()
    transforms_path = source_dir / "transforms.json"
    transforms = load_json_strict(transforms_path)
    ensure_finite(transforms)
    frames = transforms.get("frames")
    if not isinstance(frames, list) or not frames:
        raise ValueError("transforms.json must contain a non-empty frames list")

    rgb_dir = out_dir / "rgb"
    depth_dir = out_dir / "depth"
    metadata_dir = out_dir / "metadata"
    if copy_files:
        rgb_dir.mkdir(parents=True, exist_ok=True)
        depth_dir.mkdir(parents=True, exist_ok=True)
    metadata_dir.mkdir(parents=True, exist_ok=True)

    capture_frames: list[dict[str, Any]] = []
    warnings: list[str] = []
    copied_depth_count = 0
    first_intrinsics: dict[str, float] | None = None

    for index, frame in enumerate(frames, start=1):
        if not isinstance(frame, dict):
            raise ValueError(f"frame {index} is not an object")
        image_path = _resolve(source_dir, frame.get("file_path") or frame.get("rgb") or frame.get("image"))
        if image_path is None:
            raise ValueError(f"frame {index} is missing file_path")
        intrinsics = _intrinsics(transforms, frame, image_path)
        first_intrinsics = first_intrinsics or intrinsics
        rgb = _copy(image_path, rgb_dir, index, IMAGE_SUFFIXES, ".jpg") if copy_files else str(image_path)

        capture_frame: dict[str, Any] = {
            "rgb": rgb,
            "timestamp": float(frame.get("timestamp", index - 1)),
            "transform_matrix": _matrix(frame),
            "intrinsics": intrinsics,
            "capture_quality": {
                "accepted": True,
                "reason": "imported_transforms",
                "score": 1.0,
            },
        }
        depth_path = _depth_path(source_dir, frame)
        if depth_path is not None:
            capture_frame["depth"] = _copy(depth_path, depth_dir, index, DEPTH_SUFFIXES, depth_path.suffix.lower() or ".npy") if copy_files else str(depth_path)
            copied_depth_count += 1
        elif require_depth:
            raise FileNotFoundError(f"frame {index} is missing a depth path")
        else:
            warnings.append(f"frame_{index:06d}_depth_missing")
        capture_frames.append(capture_frame)

    capture = {
        "schema": CAPTURE_SCHEMA,
        "source": "transforms_import",
        "source_format": "nerfstudio_transforms",
        "intrinsics": first_intrinsics,
        "depth_scale": float(transforms.get("depth_scale", transforms.get("depth_unit_scale_factor", 1.0))),
        "frames": capture_frames,
        "authority": {
            "proposal_only": True,
            "metric_authority": False,
            "collision_geometry": False,
            "planning_authority": False,
            "semantic_authority": False,
        },
    }
    write_json_strict(out_dir / "capture.json", capture)
    load_capture(out_dir)

    summary = {
        "schema": "capture_splat.transforms_import_summary.v0.1",
        "source_dir": str(source_dir),
        "output_dir": str(out_dir),
        "frame_count": len(capture_frames),
        "depth_frame_count": copied_depth_count,
        "copied_files": copy_files,
        "warnings": warnings,
        "outputs": {
            "capture_manifest": str(out_dir / "capture.json"),
            "rgb_dir": str(rgb_dir),
            "depth_dir": str(depth_dir),
        },
        "authority": {
            "format_conversion_only": True,
            "quality_proxy": False,
            "reconstruction_quality_proof": False,
        },
    }
    write_json_strict(out_dir / "capture_splat_transforms_import_summary.json", summary)
    return summary


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Convert Nerfstudio/Record3D-style transforms.json exports into a Capture Splat capture package.")
    parser.add_argument("--input", type=Path, required=True, help="Directory containing transforms.json")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--no-copy-files", action="store_true")
    parser.add_argument("--require-depth", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    summary = import_transforms_package(
        args.input,
        args.out,
        copy_files=not args.no_copy_files,
        require_depth=args.require_depth,
    )
    print(summary["outputs"]["capture_manifest"])


if __name__ == "__main__":
    main()
