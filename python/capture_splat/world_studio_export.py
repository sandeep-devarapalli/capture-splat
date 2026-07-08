from __future__ import annotations

import hashlib
import json
import math
import shutil
from pathlib import Path
from typing import Any

import numpy as np

from .json_utils import load_json_strict, write_json_strict
from .scene_transform import SIDECAR_NAME

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp"}
MANIFEST_NAME = "capture-splat.world-studio.json"
SCHEMA = "capture_splat.world_studio_handoff.v0.2"
CAPTURE_PROFILES = ("object", "room_interior", "walkthrough", "outdoor", "video_360")


def _ply_positions(path: Path, sample_cap: int = 200_000) -> np.ndarray | None:
    try:
        with path.open("rb") as handle:
            header_lines = []
            while True:
                raw = handle.readline()
                if raw == b"":
                    return None
                line = raw.decode("ascii", errors="replace").strip()
                header_lines.append(line)
                if line == "end_header":
                    break
            offset = handle.tell()
    except OSError:
        return None
    fmt = next((line.split()[1] for line in header_lines if line.startswith("format ")), None)
    props = [line.split() for line in header_lines if line.startswith("property ")]
    if fmt == "binary_little_endian" and props and all(part[1] == "float" for part in props):
        count = int(next(line.split()[2] for line in header_lines if line.startswith("element vertex ")))
        data = np.fromfile(path, dtype="<f4", offset=offset, count=count * len(props))
        if len(data) < count * len(props):
            return None
        return data.reshape(count, len(props))[:, :3]
    if fmt == "ascii":
        rows = []
        for line in path.read_text(encoding="ascii").split("end_header", 1)[1].strip().splitlines():
            parts = line.split()
            if len(parts) >= 3:
                rows.append([float(parts[0]), float(parts[1]), float(parts[2])])
            if len(rows) >= sample_cap:
                break
        return np.asarray(rows) if rows else None
    return None


def _scene_extent(gaussian: Path | None) -> dict[str, float] | None:
    if gaussian is None or gaussian.suffix.lower() != ".ply":
        return None
    positions = _ply_positions(gaussian)
    if positions is None or len(positions) < 8:
        return None
    stride = max(1, len(positions) // 200_000)
    sampled = positions[::stride]
    center = np.median(sampled, axis=0)
    distances = np.linalg.norm(sampled - center, axis=1)
    finite = distances[np.isfinite(distances)]
    if len(finite) < 8:
        return None
    return {
        "scene_radius": float(np.percentile(finite, 95)),
        "median_structure_distance": float(np.percentile(finite, 50)),
    }


def _scene_transform_sidecar(gaussian: Path | None) -> dict[str, Any] | None:
    if gaussian is None:
        return None
    sidecar_path = gaussian.resolve().parent / SIDECAR_NAME
    if not sidecar_path.exists():
        return None
    try:
        sidecar = load_json_strict(sidecar_path)
    except ValueError:
        return None
    return sidecar if isinstance(sidecar, dict) else None


def _first_frame_camera_center(sparse_dir: Path | None) -> list[float] | None:
    if sparse_dir is None:
        return None
    images_txt = sparse_dir / "images.txt"
    if not images_txt.exists():
        return None
    for line in images_txt.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) < 10 or line.startswith("#"):
            continue
        try:
            float(parts[9])
            continue
        except ValueError:
            pass
        qw, qx, qy, qz, tx, ty, tz = (float(value) for value in parts[1:8])
        norm = math.sqrt(qw * qw + qx * qx + qy * qy + qz * qz) or 1.0
        qw, qx, qy, qz = qw / norm, qx / norm, qy / norm, qz / norm
        rotation = [
            [1 - 2 * (qy * qy + qz * qz), 2 * (qx * qy - qw * qz), 2 * (qx * qz + qw * qy)],
            [2 * (qx * qy + qw * qz), 1 - 2 * (qx * qx + qz * qz), 2 * (qy * qz - qw * qx)],
            [2 * (qx * qz - qw * qy), 2 * (qy * qz + qw * qx), 1 - 2 * (qx * qx + qy * qy)],
        ]
        return [
            -(rotation[0][0] * tx + rotation[1][0] * ty + rotation[2][0] * tz),
            -(rotation[0][1] * tx + rotation[1][1] * ty + rotation[2][1] * tz),
            -(rotation[0][2] * tx + rotation[1][2] * ty + rotation[2][2] * tz),
        ]
    return None


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
    capture_profile: str | None = None,
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
    scene_transform = _scene_transform_sidecar(gaussian)
    if scene_transform is not None:
        manifest["scene_transform"] = scene_transform
        if dataparser_transform is None and isinstance(scene_transform.get("trainer_transform"), list):
            manifest["dataparser_transform"] = scene_transform["trainer_transform"]
    extent = _scene_extent(gaussian)
    if extent is not None:
        manifest.update(extent)
    profile = capture_profile
    if profile is None and capture_manifest is not None and capture_manifest.exists():
        try:
            capture_data = json.loads(capture_manifest.read_text(encoding="utf-8"))
            candidate = capture_data.get("capture_profile") if isinstance(capture_data, dict) else None
            profile = candidate if isinstance(candidate, str) else None
        except (OSError, ValueError):
            profile = None
    if profile in CAPTURE_PROFILES:
        manifest["capture_profile"] = profile
    first_center = _first_frame_camera_center(out_dir / sparse_dir_name if copied_sparse else None)
    if first_center is not None:
        manifest["initial_camera"] = {
            "position": first_center,
            "coordinate_frame": "colmap_world",
            "mode": "orbit" if profile == "object" else "inside",
        }
    write_json_strict(out_dir / MANIFEST_NAME, manifest)
    return manifest
