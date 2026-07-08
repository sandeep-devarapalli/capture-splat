from __future__ import annotations

import hashlib
import json
import math
import shutil
from pathlib import Path
from typing import Any

from .json_utils import write_json_strict

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp"}
MANIFEST_NAME = "capture-splat.world-studio.json"
SCHEMA = "capture_splat.world_studio_handoff.v0.1"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _file_ref(path: Path, root: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(path)
    return {
        "path": path.relative_to(root).as_posix(),
        "size_bytes": path.stat().st_size,
        "checksum": _sha256(path),
    }


def _copy_or_link(src: Path, dst: Path, copy_files: bool) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.resolve() == dst.resolve():
        return
    if copy_files:
        if dst.exists() or dst.is_symlink():
            dst.unlink()
        shutil.copy2(src, dst)
    else:
        if dst.exists() or dst.is_symlink():
            dst.unlink()
        dst.symlink_to(src)


def _find_images(package: Path, image_dir_name: str) -> list[Path]:
    image_dir = package / image_dir_name
    if not image_dir.exists():
        return []
    return sorted(path for path in image_dir.iterdir() if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES)


def _copy_images(images: list[Path], out_dir: Path, copy_files: bool) -> list[Path]:
    copied: list[Path] = []
    image_out = out_dir / "images"
    for src in images:
        dst = image_out / src.name
        _copy_or_link(src, dst, copy_files)
        copied.append(dst)
    return copied


def _copy_asset(src: Path | None, out_dir: Path, name: str, copy_files: bool) -> Path | None:
    if src is None:
        return None
    src = src.resolve()
    if not src.exists():
        raise FileNotFoundError(src)
    dst = out_dir / name
    _copy_or_link(src, dst, copy_files)
    return dst


def _first_existing(root: Path, names: tuple[str, ...]) -> Path | None:
    for name in names:
        path = root / name
        if path.exists():
            return path
    return None


def _copy_sparse_dir(package: Path, out_dir: Path, sparse_dir_name: str, copy_files: bool) -> Path | None:
    sparse = package / sparse_dir_name
    if not sparse.exists():
        return None
    copied_any = False
    for name in ("cameras.txt", "images.txt", "points3D.txt"):
        src = sparse / name
        if src.exists():
            _copy_or_link(src, out_dir / sparse_dir_name / name, copy_files)
            copied_any = True
    return out_dir / sparse_dir_name if copied_any else None


def _frames(images: list[Path], out_dir: Path) -> list[dict[str, Any]]:
    frames = []
    for index, path in enumerate(images, start=1):
        frames.append({
            "frame_id": path.stem,
            "display_name": path.stem,
            "rgb_path": path.relative_to(out_dir).as_posix(),
            "source_role": "visual_evidence",
            "index": index,
            "size_bytes": path.stat().st_size,
            "checksum": _sha256(path),
        })
    return frames


def _dataparser_transform(gaussian: Path | None) -> list[list[float]] | None:
    """Trainer world transform from train.json next to the trained PLY.

    VkSplat-style trainers optimize splats in a normalized world; viewers need
    this 4x4 row-major matrix to map raw COLMAP poses into the splat world.
    """
    if gaussian is None:
        return None
    train_json = gaussian.resolve().parent / "train.json"
    if not train_json.exists():
        return None
    try:
        meta = json.loads(train_json.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = meta.get("dataparser_transform")
    if not isinstance(value, list) or len(value) != 4:
        return None
    rows: list[list[float]] = []
    for row in value:
        if not isinstance(row, list) or len(row) != 4:
            return None
        floats = [float(entry) for entry in row]
        if not all(math.isfinite(entry) for entry in floats):
            return None
        rows.append(floats)
    return rows


def export_world_studio_handoff(
    package: Path,
    out_dir: Path,
    gaussian: Path | None = None,
    points: Path | None = None,
    capture_manifest: Path | None = None,
    transforms: Path | None = None,
    poses: Path | None = None,
    camera_poses: Path | None = None,
    splat: Path | None = None,
    spz: Path | None = None,
    image_dir_name: str = "images",
    sparse_dir_name: str = "sparse/0",
    copy_files: bool = False,
) -> dict[str, Any]:
    package = package.resolve()
    out_dir = out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    images = _find_images(package, image_dir_name)
    if not images:
        raise FileNotFoundError(f"no source images found in {package / image_dir_name}")
    gaussian = gaussian or _first_existing(package, (
        "splat.pruned_a12.ply",
        "gaussians.pruned_a12.ply",
        "gaussian.pruned_a12.ply",
        "splat.ply",
        "gaussians.ply",
        "gaussian.ply",
    ))
    gaussian_variant = "alpha_pruned" if gaussian is not None and ".pruned_a" in gaussian.name else "raw"
    points = points or _first_existing(package, ("points.ply", "point_cloud.ply", "cloud.ply"))
    capture_manifest = capture_manifest or _first_existing(package, ("capture.json",))
    transforms = transforms or _first_existing(package, ("transforms.json",))
    poses = poses or _first_existing(package, ("poses.json",))
    camera_poses = camera_poses or _first_existing(package, ("camera_poses.json", "camera-poses.json"))
    splat = splat or _first_existing(package, ("scene.splat", "splat.splat"))
    spz = spz or _first_existing(package, ("scene.spz", "splat.spz"))

    copied_images = _copy_images(images, out_dir, copy_files)
    copied_sparse = _copy_sparse_dir(package, out_dir, sparse_dir_name, copy_files)
    copied_gaussian = _copy_asset(gaussian, out_dir, "splat.ply" if gaussian and gaussian.suffix.lower() == ".ply" else f"gaussian{gaussian.suffix.lower()}" if gaussian else "splat.ply", copy_files)
    copied_points = _copy_asset(points, out_dir, "points.ply", copy_files)
    copied_capture = _copy_asset(capture_manifest, out_dir, "capture.json", copy_files)
    copied_transforms = _copy_asset(transforms, out_dir, "transforms.json", copy_files)
    copied_poses = _copy_asset(poses, out_dir, Path(poses).name if poses else "poses.json", copy_files)
    copied_camera_poses = _copy_asset(camera_poses, out_dir, Path(camera_poses).name if camera_poses else "camera_poses.json", copy_files)
    copied_splat = _copy_asset(splat, out_dir, f"splat{splat.suffix.lower()}" if splat else "splat.splat", copy_files)
    copied_spz = _copy_asset(spz, out_dir, f"scene{spz.suffix.lower()}" if spz else "scene.spz", copy_files)

    assets: dict[str, Any] = {}
    if copied_points:
        assets["points"] = _file_ref(copied_points, out_dir)
    if copied_gaussian:
        gaussian_ref = _file_ref(copied_gaussian, out_dir)
        gaussian_ref["variant"] = gaussian_variant
        if gaussian_variant == "alpha_pruned":
            gaussian_ref["source_name"] = gaussian.name if gaussian else None
        assets["gaussian_ply" if copied_gaussian.suffix.lower() == ".ply" else "gaussian"] = gaussian_ref
    if copied_capture:
        assets["capture_manifest"] = _file_ref(copied_capture, out_dir)
    if copied_transforms:
        assets["transforms"] = _file_ref(copied_transforms, out_dir)
    if copied_poses:
        assets["poses"] = _file_ref(copied_poses, out_dir)
    if copied_camera_poses:
        assets["camera_poses"] = _file_ref(copied_camera_poses, out_dir)
    if copied_sparse:
        assets["colmap_sparse"] = {
            name: _file_ref(out_dir / sparse_dir_name / name, out_dir)
            for name in ("cameras.txt", "images.txt", "points3D.txt")
            if (out_dir / sparse_dir_name / name).exists()
        }
    if copied_splat:
        assets["splat"] = _file_ref(copied_splat, out_dir)
    if copied_spz:
        assets["spz"] = _file_ref(copied_spz, out_dir)

    manifest = {
        "schema": SCHEMA,
        "status": "visual_evidence_with_3dgs_proposal",
        "source_package": package.name,
        "source_frames": _frames(copied_images, out_dir),
        "frames": _frames(copied_images, out_dir),
        "assets": assets,
        "authority": {
            "source_frames": "visual_evidence",
            "trained_splats": "review_proposal",
            "metric_authority": False,
            "collision_authority": False,
            "semantic_authority": False,
            "navigation_authority": False,
            "quality_claim": False,
        },
        "notes": [
            "Source frames are visual evidence.",
            "Trained splats are review proposals, not metric, collision, semantic, or navigation authority.",
        ],
    }
    dataparser_transform = _dataparser_transform(gaussian)
    if dataparser_transform is not None:
        manifest["dataparser_transform"] = dataparser_transform
        manifest["notes"].append(
            "dataparser_transform maps raw COLMAP world poses into the trained splat world (row-major 4x4, from the trainer's train.json)."
        )
    write_json_strict(out_dir / MANIFEST_NAME, manifest)
    return manifest
