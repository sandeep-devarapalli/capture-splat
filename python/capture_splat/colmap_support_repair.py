from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from .json_utils import load_json_strict, write_json_strict
from .weak_frames_report import _frame_key, _parse_colmap_images


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png"}


def _frame_number(value: str | Path) -> int | None:
    key = _frame_key(value)
    return int(key) if key.isdigit() else None


def _package_images(package: Path, image_dir_name: str) -> dict[str, str]:
    image_dir = package / image_dir_name
    if not image_dir.exists():
        raise FileNotFoundError(f"image directory missing: {image_dir}")
    images: dict[str, str] = {}
    for path in sorted(image_dir.iterdir()):
        if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS:
            images[_frame_key(path.name)] = path.name
    if not images:
        raise ValueError(f"no images found in {image_dir}")
    return images


def _capture_quality_by_frame(capture: Path | None) -> dict[str, dict[str, Any]]:
    if capture is None:
        return {}
    path = capture / "capture.json" if capture.is_dir() else capture
    if not path.exists():
        return {}
    data = load_json_strict(path)
    frames = data.get("frames") if isinstance(data, dict) else None
    if not isinstance(frames, list):
        return {}
    by_id: dict[str, dict[str, Any]] = {}
    for frame in frames:
        if not isinstance(frame, dict):
            continue
        image = frame.get("rgb") or frame.get("image") or frame.get("image_path") or frame.get("file_path")
        quality = frame.get("capture_quality") or frame.get("quality") or {}
        if isinstance(image, str) and isinstance(quality, dict):
            by_id[_frame_key(image)] = quality
    return by_id


def _nearby_frame_keys(target: str, images: dict[str, str], radius: int) -> list[str]:
    number = _frame_number(target)
    if number is None:
        return [target] if target in images else []
    keys = []
    for value in range(number - radius, number + radius + 1):
        key = f"{value:06d}"
        if key in images:
            keys.append(key)
    return keys


def _strong_anchor_keys(
    colmap_support: dict[str, dict[str, Any]],
    min_observations: int,
    min_observation_ratio: float,
) -> list[str]:
    anchors = []
    for key, support in colmap_support.items():
        observations = int(support.get("observation_count") or 0)
        ratio = support.get("valid_observation_ratio")
        if isinstance(ratio, (float, int)) and observations >= min_observations and float(ratio) >= min_observation_ratio:
            anchors.append(key)
    return sorted(anchors, key=lambda item: _frame_number(item) if _frame_number(item) is not None else 10**12)


def _nearest_anchors(target: str, anchors: list[str], limit: int) -> list[str]:
    target_number = _frame_number(target)
    if target_number is None:
        return anchors[:limit]
    return sorted(
        anchors,
        key=lambda item: abs((_frame_number(item) or target_number) - target_number),
    )[:limit]


def _pair(a: str, b: str, images: dict[str, str]) -> tuple[str, str] | None:
    if a == b or a not in images or b not in images:
        return None
    left, right = sorted((images[a], images[b]), key=_natural_sort_key)
    return left, right


def _natural_sort_key(value: str) -> list[int | str]:
    parts = re.split(r"(\d+)", value)
    return [int(part) if part.isdigit() else part for part in parts]


def build_colmap_support_repair(
    weak_report: Path,
    package: Path,
    out_dir: Path,
    capture: Path | None = None,
    colmap_images: Path | None = None,
    image_dir_name: str = "images",
    neighbor_radius: int = 4,
    max_anchors_per_target: int = 8,
    min_colmap_observations: int = 100,
    min_colmap_observation_ratio: float = 0.10,
) -> dict[str, Any]:
    weak_report = weak_report.resolve()
    package = package.resolve()
    out_dir = out_dir.resolve()
    if neighbor_radius < 0:
        raise ValueError("neighbor_radius must be non-negative")
    if max_anchors_per_target < 0:
        raise ValueError("max_anchors_per_target must be non-negative")
    report = load_json_strict(weak_report)
    frames = report.get("frames") if isinstance(report, dict) else None
    if not isinstance(frames, list):
        raise ValueError("weak report must contain a frames list")

    images = _package_images(package, image_dir_name)
    colmap_path = colmap_images or package / "sparse" / "0" / "images.txt"
    colmap_support = _parse_colmap_images(colmap_path.resolve()) if colmap_path.exists() else {}
    capture_quality = _capture_quality_by_frame(capture.resolve() if capture else None)
    anchors = _strong_anchor_keys(colmap_support, min_colmap_observations, min_colmap_observation_ratio)

    target_items: list[dict[str, Any]] = []
    selected_keys: set[str] = set()
    pairs: set[tuple[str, str]] = set()
    for frame in frames:
        if not isinstance(frame, dict):
            continue
        frame_id = str(frame.get("frame_id") or "")
        key = _frame_key(frame_id)
        if key not in images:
            continue
        reasons = [str(item) for item in frame.get("possible_reason_buckets", []) if isinstance(item, str)]
        priority = "high" if "weak_colmap_support" in reasons else "medium"
        nearby = _nearby_frame_keys(key, images, neighbor_radius)
        nearest = [anchor for anchor in _nearest_anchors(key, anchors, max_anchors_per_target) if anchor != key]
        selected_keys.update(nearby)
        selected_keys.update(nearest)
        selected_keys.add(key)
        for other in nearby + nearest:
            pair = _pair(key, other, images)
            if pair:
                pairs.add(pair)
        for left, right in zip(nearby, nearby[1:]):
            pair = _pair(left, right, images)
            if pair:
                pairs.add(pair)
        support = colmap_support.get(key)
        target_items.append({
            "frame_id": frame_id,
            "image": images[key],
            "priority": priority,
            "weak_reasons": frame.get("qa_metrics", {}).get("weak_reasons", []),
            "possible_reason_buckets": reasons,
            "colmap_support": support,
            "capture_quality": capture_quality.get(key),
            "neighbor_images": [images[item] for item in nearby],
            "anchor_images": [images[item] for item in nearest],
        })

    selected_images = sorted((images[key] for key in selected_keys if key in images), key=_natural_sort_key)
    pair_rows = sorted(pairs, key=lambda item: (_natural_sort_key(item[0]), _natural_sort_key(item[1])))
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "repair_image_list.txt").write_text("\n".join(selected_images) + "\n", encoding="utf-8")
    (out_dir / "repair_pairs.txt").write_text("\n".join(f"{left} {right}" for left, right in pair_rows) + "\n", encoding="utf-8")
    summary = {
        "schema": "capture_splat.colmap_support_repair.v0.1",
        "decision": "hold",
        "authority": {
            "diagnostic_only": True,
            "colmap_repair_complete": False,
            "training_result": False,
            "quality_claim": False,
        },
        "weak_report": str(weak_report),
        "package": str(package),
        "capture": str(capture.resolve()) if capture else None,
        "colmap_images": str(colmap_path.resolve()) if colmap_path.exists() else None,
        "thresholds": {
            "neighbor_radius": neighbor_radius,
            "max_anchors_per_target": max_anchors_per_target,
            "min_colmap_observations": min_colmap_observations,
            "min_colmap_observation_ratio": min_colmap_observation_ratio,
        },
        "target_count": len(target_items),
        "selected_image_count": len(selected_images),
        "pair_count": len(pair_rows),
        "targets": target_items,
        "outputs": {
            "repair_image_list": str(out_dir / "repair_image_list.txt"),
            "repair_pairs": str(out_dir / "repair_pairs.txt"),
        },
        "recommended_next_steps": [
            "rerun COLMAP sparse reconstruction with guided matching focused on these targets and neighbors",
            "recompute per-frame COLMAP support from the new images.txt",
            "rerun VkSplat 3000 then 7000 only if weak target support improves",
        ],
    }
    write_json_strict(out_dir / "capture_splat_colmap_support_repair_manifest.json", summary)
    return summary


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a COLMAP support repair manifest from weak render/source frames.")
    parser.add_argument("--weak-report", type=Path, required=True)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--capture", type=Path)
    parser.add_argument("--colmap-images", type=Path)
    parser.add_argument("--image-dir", default="images")
    parser.add_argument("--neighbor-radius", type=int, default=4)
    parser.add_argument("--max-anchors-per-target", type=int, default=8)
    parser.add_argument("--min-colmap-observations", type=int, default=100)
    parser.add_argument("--min-colmap-observation-ratio", type=float, default=0.10)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    summary = build_colmap_support_repair(
        args.weak_report,
        args.package,
        args.out,
        capture=args.capture,
        colmap_images=args.colmap_images,
        image_dir_name=args.image_dir,
        neighbor_radius=args.neighbor_radius,
        max_anchors_per_target=args.max_anchors_per_target,
        min_colmap_observations=args.min_colmap_observations,
        min_colmap_observation_ratio=args.min_colmap_observation_ratio,
    )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
