from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from .json_utils import load_json_strict, write_json_strict


def _frame_key(value: str | Path) -> str:
    stem = Path(str(value)).stem
    matches = re.findall(r"\d+", stem)
    return matches[-1] if matches else stem


def _parse_colmap_images(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip() and not line.startswith("#")]
    support: dict[str, dict[str, Any]] = {}
    index = 0
    while index + 1 < len(lines):
        header = lines[index].split()
        points = lines[index + 1].split()
        index += 2
        if len(header) < 10:
            continue
        try:
            image_id = int(header[0])
        except ValueError:
            continue
        name = header[9]
        point_ids = points[2::3]
        valid = [point_id for point_id in point_ids if point_id != "-1"]
        support[_frame_key(name)] = {
            "image_id": image_id,
            "image_name": name,
            "registered": True,
            "feature_count": len(point_ids),
            "observation_count": len(valid),
            "valid_observation_ratio": (len(valid) / len(point_ids)) if point_ids else None,
            "unique_point_count": len(set(valid)),
        }
    return support


def _parse_capture_frames(capture_path: Path | None) -> dict[str, dict[str, Any]]:
    if capture_path is None:
        return {}
    path = capture_path / "capture.json" if capture_path.is_dir() else capture_path
    if not path.exists():
        return {}
    capture = load_json_strict(path)
    frames = capture.get("frames") if isinstance(capture, dict) else None
    if not isinstance(frames, list):
        return {}
    by_id = {}
    for frame in frames:
        if not isinstance(frame, dict):
            continue
        rgb = frame.get("rgb")
        if not isinstance(rgb, str):
            continue
        quality = frame.get("capture_quality") or frame.get("quality") or {}
        by_id[_frame_key(rgb)] = quality if isinstance(quality, dict) else {}
    return by_id


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, (float, int)) and math.isfinite(float(value)) else None


def _ratio(numerator: Any, denominator: Any) -> float | None:
    n = _num(numerator)
    d = _num(denominator)
    if n is None or d is None or d <= 0.0:
        return None
    return n / d


def _reason_buckets(
    frame: dict[str, Any],
    colmap: dict[str, Any] | None,
    capture_quality: dict[str, Any] | None,
    min_colmap_observations: int,
    min_colmap_observation_ratio: float,
    min_blur_score: float,
    min_parallax_meters: float,
    min_overlap_score: float,
    max_clipped_fraction: float,
) -> list[str]:
    reasons = []
    if colmap is None:
        reasons.append("colmap_support_missing")
    else:
        observations = int(colmap.get("observation_count") or 0)
        if observations < min_colmap_observations:
            reasons.append("weak_colmap_support")
        ratio = _num(colmap.get("valid_observation_ratio"))
        if ratio is not None and ratio < min_colmap_observation_ratio and "weak_colmap_support" not in reasons:
            reasons.append("weak_colmap_support")

    render_sharpness_ratio = _ratio(frame.get("render_laplacian_variance"), frame.get("source_laplacian_variance"))
    render_edge_ratio = _ratio(frame.get("render_edge_density"), frame.get("source_edge_density"))
    if render_sharpness_ratio is not None and render_sharpness_ratio < 0.25:
        reasons.append("render_sharpness_below_source")
    if render_edge_ratio is not None and render_edge_ratio < 0.5:
        reasons.append("render_edge_density_below_source")

    source_lap = _num(frame.get("source_laplacian_variance"))
    source_edge = _num(frame.get("source_edge_density"))
    if source_lap is not None and source_lap < 0.0008:
        reasons.append("low_source_detail")
    if source_edge is not None and source_edge < 0.006:
        reasons.append("low_source_edge_density")

    if capture_quality:
        blur = _num(capture_quality.get("blur_score"))
        if blur is not None and blur < min_blur_score:
            reasons.append("capture_blur_proxy_low")
        parallax = _num(capture_quality.get("parallax_meters"))
        if parallax is not None and parallax < min_parallax_meters:
            reasons.append("capture_parallax_low")
        overlap = _num(capture_quality.get("colmap_overlap_score"))
        if overlap is not None and overlap < min_overlap_score:
            reasons.append("capture_overlap_low")
        highlight = _num(capture_quality.get("clipped_highlight_fraction")) or 0.0
        shadow = _num(capture_quality.get("clipped_shadow_fraction")) or 0.0
        if max(highlight, shadow) > max_clipped_fraction:
            reasons.append("capture_exposure_clipping")
    return reasons


def _make_contact_sheet(frames: list[dict[str, Any]], out_path: Path) -> None:
    if not frames:
        return
    thumb_w, thumb_h = 280, 210
    label_h = 44
    margin = 10
    rows = len(frames)
    cols = 2
    width = margin + cols * thumb_w + (cols + 1) * margin
    height = margin + rows * (thumb_h + label_h + margin)
    canvas = Image.new("RGB", (width, height), (24, 24, 24))
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.load_default()
    for row, frame in enumerate(frames):
        y = margin + row * (thumb_h + label_h + margin)
        for col, key in enumerate(("source", "render")):
            x = margin + col * (thumb_w + margin)
            try:
                with Image.open(frame[key]) as image:
                    image = image.convert("RGB")
                    image.thumbnail((thumb_w, thumb_h), Image.Resampling.LANCZOS)
                    tile = Image.new("RGB", (thumb_w, thumb_h), (0, 0, 0))
                    tile.paste(image, ((thumb_w - image.width) // 2, (thumb_h - image.height) // 2))
                    canvas.paste(tile, (x, y + label_h))
            except Exception:
                draw.rectangle((x, y + label_h, x + thumb_w, y + label_h + thumb_h), outline=(160, 40, 40))
            if row == 0:
                draw.text((x, y), key.upper(), fill=(230, 230, 230), font=font)
        label = f"{frame['frame_id']} PSNR {frame.get('psnr', 0):.1f} SSIM {frame.get('ssim', 0):.2f}"
        draw.text((margin, y + 16), label, fill=(230, 230, 230), font=font)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_path)


def run_weak_frames_report(
    qa_summary_path: Path,
    out_dir: Path,
    colmap_images: Path | None = None,
    capture: Path | None = None,
    min_colmap_observations: int = 100,
    min_colmap_observation_ratio: float = 0.10,
    min_blur_score: float = 0.006,
    min_parallax_meters: float = 0.05,
    min_overlap_score: float = 0.45,
    max_clipped_fraction: float = 0.02,
    max_contact_frames: int = 12,
) -> dict[str, Any]:
    qa_summary_path = qa_summary_path.resolve()
    out_dir = out_dir.resolve()
    qa = load_json_strict(qa_summary_path)
    if not isinstance(qa, dict):
        raise ValueError("QA summary must be a JSON object")
    frames = [frame for frame in qa.get("frames", []) if isinstance(frame, dict)]
    weak_ids = {str(frame_id) for frame_id in qa.get("weak_frames", []) if isinstance(frame_id, str)}
    tail_ids = {str(frame_id) for frame_id in qa.get("tail_frames", []) if isinstance(frame_id, str)}
    selected = [frame for frame in frames if str(frame.get("frame_id")) in weak_ids or str(frame.get("frame_id")) in tail_ids]
    selected.sort(key=lambda frame: (str(frame.get("frame_id")) not in tail_ids, float(frame.get("psnr", 999.0))))

    colmap_support = _parse_colmap_images(colmap_images.resolve()) if colmap_images else {}
    capture_by_id = _parse_capture_frames(capture.resolve() if capture else None)
    diagnostics = []
    for frame in selected:
        frame_id = str(frame.get("frame_id"))
        colmap = colmap_support.get(_frame_key(frame_id))
        quality = capture_by_id.get(_frame_key(frame_id))
        source_lap = _num(frame.get("source_laplacian_variance"))
        render_lap = _num(frame.get("render_laplacian_variance"))
        source_edge = _num(frame.get("source_edge_density"))
        render_edge = _num(frame.get("render_edge_density"))
        item = {
            "frame_id": frame_id,
            "source": frame.get("source"),
            "render": frame.get("render"),
            "qa_metrics": {
                "psnr": frame.get("psnr"),
                "ssim": frame.get("ssim"),
                "mae": frame.get("mae"),
                "normalized_correlation": frame.get("normalized_correlation"),
                "weak_reasons": frame.get("weak_reasons", []),
                "is_tail_frame": frame_id in tail_ids,
            },
            "sharpness_proxy": {
                "source_laplacian_variance": source_lap,
                "render_laplacian_variance": render_lap,
                "render_to_source_laplacian_ratio": _ratio(render_lap, source_lap),
                "source_edge_density": source_edge,
                "render_edge_density": render_edge,
                "render_to_source_edge_ratio": _ratio(render_edge, source_edge),
            },
            "colmap_support": colmap,
            "capture_quality": quality,
            "possible_reason_buckets": _reason_buckets(
                frame,
                colmap,
                quality,
                min_colmap_observations,
                min_colmap_observation_ratio,
                min_blur_score,
                min_parallax_meters,
                min_overlap_score,
                max_clipped_fraction,
            ),
        }
        diagnostics.append(item)

    reason_counts: dict[str, int] = {}
    for item in diagnostics:
        for reason in item["possible_reason_buckets"]:
            reason_counts[reason] = reason_counts.get(reason, 0) + 1

    contact_frames = selected[:max_contact_frames]
    contact_sheet = out_dir / "weak_frames_contact_sheet.png"
    if contact_frames:
        _make_contact_sheet(contact_frames, contact_sheet)

    summary = {
        "schema": "capture_splat.weak_frames_report.v0.1",
        "qa_summary": str(qa_summary_path),
        "colmap_images": str(colmap_images.resolve()) if colmap_images else None,
        "capture": str(capture.resolve()) if capture else None,
        "thresholds": {
            "min_colmap_observations": min_colmap_observations,
            "min_colmap_observation_ratio": min_colmap_observation_ratio,
            "min_blur_score": min_blur_score,
            "min_parallax_meters": min_parallax_meters,
            "min_overlap_score": min_overlap_score,
            "max_clipped_fraction": max_clipped_fraction,
        },
        "frame_count": len(frames),
        "weak_frame_count": len(weak_ids),
        "tail_frame_count": len(tail_ids),
        "diagnosed_frame_count": len(diagnostics),
        "reason_counts": dict(sorted(reason_counts.items())),
        "contact_sheet": str(contact_sheet) if contact_frames else None,
        "decision": "hold" if diagnostics else "promote",
        "authority": {
            "diagnostic_only": True,
            "quality_proxy": True,
            "quality_claim": False,
            "metric_authority": False,
        },
        "frames": diagnostics,
    }
    write_json_strict(out_dir / "capture_splat_weak_frames_report.json", summary)
    return summary


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Diagnose weak render/source QA frames with COLMAP and capture proxies.")
    parser.add_argument("--qa-summary", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--colmap-images", type=Path)
    parser.add_argument("--capture", type=Path)
    parser.add_argument("--min-colmap-observations", type=int, default=100)
    parser.add_argument("--min-colmap-observation-ratio", type=float, default=0.10)
    parser.add_argument("--min-blur-score", type=float, default=0.006)
    parser.add_argument("--min-parallax-meters", type=float, default=0.05)
    parser.add_argument("--min-overlap-score", type=float, default=0.45)
    parser.add_argument("--max-clipped-fraction", type=float, default=0.02)
    parser.add_argument("--max-contact-frames", type=int, default=12)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    summary = run_weak_frames_report(
        args.qa_summary,
        args.out,
        colmap_images=args.colmap_images,
        capture=args.capture,
        min_colmap_observations=args.min_colmap_observations,
        min_colmap_observation_ratio=args.min_colmap_observation_ratio,
        min_blur_score=args.min_blur_score,
        min_parallax_meters=args.min_parallax_meters,
        min_overlap_score=args.min_overlap_score,
        max_clipped_fraction=args.max_clipped_fraction,
        max_contact_frames=args.max_contact_frames,
    )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
