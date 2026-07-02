from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np

from .capture_schema import frame_selection_summary, load_capture
from .json_utils import load_json_strict, write_json_strict


def _quality(frame: dict[str, Any]) -> dict[str, Any]:
    quality = frame.get("capture_quality") or frame.get("quality")
    return quality if isinstance(quality, dict) else {}


def _stats(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"mean": None, "min": None, "max": None}
    return {
        "mean": float(np.mean(values)),
        "min": float(np.min(values)),
        "max": float(np.max(values)),
    }


def _metric_stats(frames: list[dict[str, Any]], name: str) -> dict[str, float | None]:
    values = []
    for frame in frames:
        value = _quality(frame).get(name)
        if isinstance(value, (float, int)):
            values.append(float(value))
    return _stats(values)


def _load_optional_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    data = load_json_strict(path)
    return data if isinstance(data, dict) else None


def run_capture_quality_report(
    capture_dir: Path,
    out_dir: Path,
    min_accepted_frames: int = 24,
    min_mean_blur_score: float = 0.006,
    min_mean_parallax_meters: float = 0.05,
    min_mean_overlap_score: float = 0.45,
    min_mean_depth_ratio: float = 0.35,
) -> dict[str, Any]:
    capture_dir = capture_dir.resolve()
    out_dir = out_dir.resolve()
    capture = load_capture(capture_dir)
    frames = [frame for frame in capture["frames"] if isinstance(frame, dict)]
    selection = frame_selection_summary(capture)
    quality_present = selection["quality_metadata_present"]
    accepted_frames = [frame for frame in frames if _quality(frame).get("accepted") is not False]
    rejected_frames = [frame for frame in frames if _quality(frame).get("accepted") is False]
    keyframe_report = _load_optional_json(capture_dir / "metadata" / "keyframe_report.json")
    room_report = _load_optional_json(capture_dir / "metadata" / "room_capture_quality_report.json")

    metric_names = (
        "blur_score",
        "exposure_mean",
        "exposure_delta",
        "clipped_highlight_fraction",
        "clipped_shadow_fraction",
        "feature_grid_coverage",
        "parallax_meters",
        "colmap_overlap_score",
        "valid_depth_ratio",
        "feature_point_count",
    )
    metrics = {name: _metric_stats(accepted_frames, name) for name in metric_names}
    skip_reason_counts = {}
    if keyframe_report is not None:
        counts = keyframe_report.get("skip_reason_counts")
        if isinstance(counts, dict):
            skip_reason_counts = {str(key): int(value) for key, value in counts.items() if isinstance(value, int)}

    warnings: list[str] = []
    blockers: list[str] = []
    if not quality_present:
        warnings.append("capture_quality_metadata_missing")
    if selection["selected_frames"] == 0:
        blockers.append("no_accepted_frames")
    if selection["selected_frames"] < min_accepted_frames:
        warnings.append("accepted_frame_count_below_threshold")
    blur_mean = metrics["blur_score"]["mean"]
    if blur_mean is not None and blur_mean < min_mean_blur_score:
        warnings.append("mean_blur_below_threshold")
    parallax_mean = metrics["parallax_meters"]["mean"]
    if parallax_mean is not None and parallax_mean < min_mean_parallax_meters:
        warnings.append("mean_parallax_below_threshold")
    overlap_mean = metrics["colmap_overlap_score"]["mean"]
    if overlap_mean is not None and overlap_mean < min_mean_overlap_score:
        warnings.append("mean_overlap_below_threshold")
    depth_mean = metrics["valid_depth_ratio"]["mean"]
    if depth_mean is not None and depth_mean < min_mean_depth_ratio:
        warnings.append("mean_depth_ratio_below_threshold")
    if keyframe_report is None:
        warnings.append("keyframe_report_missing")
    if room_report is None:
        warnings.append("room_quality_report_missing")

    if blockers:
        decision = "reject"
        recommendation = "recapture_before_colmap"
    elif warnings:
        decision = "hold"
        recommendation = "inspect_capture_before_training"
    else:
        decision = "promote"
        recommendation = "safe_to_try_colmap_export"

    summary = {
        "schema": "capture_splat.capture_quality_report.v0.1",
        "capture_dir": str(capture_dir),
        "thresholds": {
            "min_accepted_frames": min_accepted_frames,
            "min_mean_blur_score": min_mean_blur_score,
            "min_mean_parallax_meters": min_mean_parallax_meters,
            "min_mean_overlap_score": min_mean_overlap_score,
            "min_mean_depth_ratio": min_mean_depth_ratio,
        },
        "frame_selection": selection,
        "accepted_frame_count": len(accepted_frames),
        "rejected_frame_count": len(rejected_frames),
        "metrics": metrics,
        "skip_reason_counts": skip_reason_counts,
        "metadata": {
            "keyframe_report_present": keyframe_report is not None,
            "room_quality_report_present": room_report is not None,
            "haptic_accepted_count": keyframe_report.get("haptic_accepted_count") if keyframe_report else None,
            "reported_accepted_keyframes": keyframe_report.get("accepted_keyframes") if keyframe_report else None,
            "reported_skipped_candidates": keyframe_report.get("skipped_keyframe_candidates") if keyframe_report else None,
        },
        "warnings": warnings,
        "blockers": blockers,
        "decision": decision,
        "recommendation": recommendation,
        "authority": {
            "capture_guidance_only": True,
            "quality_proxy": True,
            "reconstruction_quality_proof": False,
            "metric_authority": False,
            "training_result": False,
        },
    }
    write_json_strict(out_dir / "capture_splat_capture_quality_report.json", summary)
    return summary


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Summarize capture-time quality before COLMAP or VkSplat training.")
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--min-accepted-frames", type=int, default=24)
    parser.add_argument("--min-mean-blur-score", type=float, default=0.006)
    parser.add_argument("--min-mean-parallax-meters", type=float, default=0.05)
    parser.add_argument("--min-mean-overlap-score", type=float, default=0.45)
    parser.add_argument("--min-mean-depth-ratio", type=float, default=0.35)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    summary = run_capture_quality_report(
        args.capture,
        args.out,
        min_accepted_frames=args.min_accepted_frames,
        min_mean_blur_score=args.min_mean_blur_score,
        min_mean_parallax_meters=args.min_mean_parallax_meters,
        min_mean_overlap_score=args.min_mean_overlap_score,
        min_mean_depth_ratio=args.min_mean_depth_ratio,
    )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
