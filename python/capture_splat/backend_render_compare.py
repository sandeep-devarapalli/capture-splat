from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .json_utils import load_json_strict, write_json_strict
from .ply_stats import inspect_ply
from .render_source_qa import run_render_source_qa

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp"}


def _normalize_frame_id(value: str) -> str:
    numbers = re.findall(r"\d+", value)
    if numbers:
        return numbers[-1].zfill(6)
    return Path(value).stem


def _image_index(image_dir: Path) -> dict[str, Path]:
    return {
        _normalize_frame_id(path.stem): path
        for path in sorted(image_dir.iterdir())
        if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES
    }


def _parse_frames(frames: str | None, frames_json: Path | None, image_dir: Path) -> list[str]:
    if frames_json is not None:
        data = load_json_strict(frames_json)
        raw_frames = data.get("frames") if isinstance(data, dict) else data
        if not isinstance(raw_frames, list):
            raise ValueError("frames JSON must be a list or contain a frames list")
        return [_normalize_frame_id(str(frame)) for frame in raw_frames]
    if frames:
        return [_normalize_frame_id(frame.strip()) for frame in frames.split(",") if frame.strip()]
    return sorted(_image_index(image_dir))


def _write_pairs(frame_ids: list[str], image_dir: Path, render_dir: Path | None, out_path: Path) -> tuple[list[dict[str, str]], list[str]]:
    images = _image_index(image_dir)
    warnings: list[str] = []
    pairs: list[dict[str, str]] = []
    for frame_id in frame_ids:
        source = images.get(frame_id)
        if source is None:
            warnings.append(f"source_frame_missing:{frame_id}")
            continue
        render_name = source.name
        if render_dir is not None:
            candidates = [
                render_dir / source.name,
                render_dir / f"{source.stem}.png",
                render_dir / f"{source.stem}.jpg",
                render_dir / f"frame_{frame_id}.png",
                render_dir / f"frame_{frame_id}.jpg",
            ]
            render = next((candidate for candidate in candidates if candidate.exists()), None)
            if render is None:
                warnings.append(f"render_frame_missing:{frame_id}")
            else:
                render_name = render.relative_to(render_dir).as_posix()
        pairs.append({
            "frame_id": frame_id,
            "source": source.relative_to(image_dir).as_posix(),
            "render": render_name,
        })
    write_json_strict(out_path, {"schema": "capture_splat.backend_render_pairs.v0.1", "pairs": pairs})
    return pairs, warnings


def _backend_summary(
    label: str,
    ply_path: Path | None,
    image_dir: Path,
    render_dir: Path | None,
    out_dir: Path,
    pairs_path: Path,
    renderer_command: str | None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "label": label,
        "render_dir": str(render_dir.resolve()) if render_dir else None,
        "renderer_command": renderer_command,
        "warnings": [],
        "decision": "hold",
    }
    if ply_path is not None:
        result["ply_path"] = str(ply_path.resolve())
        if ply_path.exists():
            result["ply_stats"] = inspect_ply(ply_path)
        else:
            result["warnings"].append("ply_missing")
            result["decision"] = "reject"
    else:
        result["warnings"].append("ply_not_provided")

    if render_dir is None:
        result["warnings"].append("renderer_missing")
        result["authority"] = {"rendered_by_command": False, "render_source_qa": False}
        return result
    if not render_dir.exists():
        result["warnings"].append("render_dir_missing")
        result["decision"] = "reject"
        result["authority"] = {"rendered_by_command": False, "render_source_qa": False}
        return result

    qa = run_render_source_qa(image_dir, render_dir, out_dir / "qa" / label, pairs_json=pairs_path)
    result["render_source_qa_summary"] = qa
    result["decision"] = qa["decision"]
    result["authority"] = {"rendered_by_command": bool(renderer_command), "render_source_qa": True}
    return result


def _metric_mean(summary: dict[str, Any], metric: str) -> float | None:
    qa = summary.get("render_source_qa_summary")
    if not isinstance(qa, dict):
        return None
    value = qa.get("aggregates", {}).get(metric, {}).get("mean")
    return float(value) if isinstance(value, (float, int)) else None


def compare_backend_renders(
    package: Path,
    out_dir: Path,
    frames: str | None = None,
    frames_json: Path | None = None,
    gsplat_ply: Path | None = None,
    vksplat_ply: Path | None = None,
    gsplat_render_dir: Path | None = None,
    vksplat_render_dir: Path | None = None,
    gsplat_renderer_command: str | None = None,
    vksplat_renderer_command: str | None = None,
    image_dir_name: str = "images",
) -> dict[str, Any]:
    package = package.resolve()
    image_dir = package / image_dir_name
    out_dir = out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    if not image_dir.exists():
        raise FileNotFoundError(f"package image directory missing: {image_dir}")

    fixed_set = package / "metadata" / "fixed_camera_evaluation_set.json"
    if not fixed_set.exists():
        raise FileNotFoundError(f"fixed-camera evaluation set missing: {fixed_set}")
    pair_warnings: list[str] = []
    fixed_frame_ids = _parse_frames(None, fixed_set, image_dir)
    if frames is not None or frames_json is not None:
        requested = _parse_frames(frames, frames_json, image_dir)
        if requested != fixed_frame_ids:
            raise ValueError("backend comparison frames must match the package fixed-camera evaluation set")
    frame_ids = fixed_frame_ids
    pairs_path = out_dir / "camera_pairs.json"
    pairs, warnings = _write_pairs(frame_ids, image_dir, None, pairs_path)
    pair_warnings.extend(warnings)

    backend_pairs: dict[str, str] = {}
    for label, render_dir in (("gsplat", gsplat_render_dir), ("vksplat", vksplat_render_dir)):
        if render_dir is None:
            continue
        path = out_dir / f"{label}_pairs.json"
        _, warnings = _write_pairs(frame_ids, image_dir, render_dir.resolve(), path)
        pair_warnings.extend(f"{label}:{warning}" for warning in warnings)
        backend_pairs[label] = str(path)

    gsplat = _backend_summary("gsplat", gsplat_ply, image_dir, gsplat_render_dir, out_dir, Path(backend_pairs.get("gsplat", pairs_path)), gsplat_renderer_command)
    vksplat = _backend_summary("vksplat", vksplat_ply, image_dir, vksplat_render_dir, out_dir, Path(backend_pairs.get("vksplat", pairs_path)), vksplat_renderer_command)

    deltas = {}
    for metric in ("psnr", "ssim", "mae", "normalized_correlation"):
        g = _metric_mean(gsplat, metric)
        v = _metric_mean(vksplat, metric)
        deltas[metric] = None if g is None or v is None else g - v

    warnings = pair_warnings + gsplat["warnings"] + vksplat["warnings"]
    decision = "reject" if gsplat["decision"] == "reject" or vksplat["decision"] == "reject" else "hold"
    if gsplat["decision"] == "promote" and vksplat["decision"] == "promote" and not warnings:
        decision = "promote"

    summary = {
        "schema": "capture_splat.backend_render_comparison.v0.1",
        "package": str(package),
        "image_dir": str(image_dir),
        "camera_pairs": str(pairs_path),
        "frame_count": len(pairs),
        "requested_frame_count": len(frame_ids),
        "fixed_camera_evaluation_set": str(fixed_set),
        "backends": {"gsplat": gsplat, "vksplat": vksplat},
        "metric_mean_deltas": deltas,
        "warnings": warnings,
        "decision": decision,
        "authority": {
            "same_frame_list": True,
            "fixed_camera_evaluation_enforced": True,
            "backend_renderers_required_for_quality_claim": True,
            "quality_claim": False,
        },
    }
    write_json_strict(out_dir / "capture_splat_backend_render_comparison.json", summary)
    return summary
