from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from .json_utils import load_json_strict, write_json_strict
from .weak_frames_report import _frame_key, _parse_colmap_images


def _frame_sort_key(value: str) -> tuple[int, int | str]:
    return (0, int(value)) if value.isdigit() else (1, value)


def _frames_from_text(value: str | None) -> list[str]:
    if not value:
        return []
    frames = []
    for item in re.split(r"[,\\s]+", value.strip()):
        if item:
            frames.append(_frame_key(item))
    return frames


def _frames_from_weak_report(path: Path | None) -> list[str]:
    if path is None:
        return []
    report = load_json_strict(path)
    frames = report.get("frames") if isinstance(report, dict) else None
    if not isinstance(frames, list):
        raise ValueError("weak report must contain a frames list")
    out = []
    for frame in frames:
        if isinstance(frame, dict):
            frame_id = frame.get("frame_id")
            if isinstance(frame_id, str):
                out.append(_frame_key(frame_id))
    return out


def _support_delta(original: dict[str, Any] | None, repaired: dict[str, Any] | None) -> dict[str, Any]:
    original_observations = int((original or {}).get("observation_count") or 0)
    repaired_observations = int((repaired or {}).get("observation_count") or 0)
    original_ratio = (original or {}).get("valid_observation_ratio")
    repaired_ratio = (repaired or {}).get("valid_observation_ratio")
    original_ratio_float = float(original_ratio) if isinstance(original_ratio, (float, int)) else None
    repaired_ratio_float = float(repaired_ratio) if isinstance(repaired_ratio, (float, int)) else None
    return {
        "original_registered": original is not None,
        "repaired_registered": repaired is not None,
        "original_observation_count": original_observations,
        "repaired_observation_count": repaired_observations,
        "delta_observation_count": repaired_observations - original_observations,
        "original_valid_observation_ratio": original_ratio_float,
        "repaired_valid_observation_ratio": repaired_ratio_float,
        "delta_valid_observation_ratio": (
            repaired_ratio_float - original_ratio_float
            if repaired_ratio_float is not None and original_ratio_float is not None
            else None
        ),
    }


def compare_colmap_support_delta(
    original_images: Path,
    repaired_images: Path,
    out_dir: Path,
    frames: str | None = None,
    weak_report: Path | None = None,
    min_observation_gain: int = 100,
    min_ratio_gain: float = 0.03,
    require_all_improved: bool = False,
) -> dict[str, Any]:
    original_images = original_images.resolve()
    repaired_images = repaired_images.resolve()
    out_dir = out_dir.resolve()
    if min_observation_gain < 0:
        raise ValueError("min_observation_gain must be non-negative")
    if min_ratio_gain < 0:
        raise ValueError("min_ratio_gain must be non-negative")
    original = _parse_colmap_images(original_images)
    repaired = _parse_colmap_images(repaired_images)
    frame_keys = sorted(set(_frames_from_text(frames) + _frames_from_weak_report(weak_report)), key=_frame_sort_key)
    if not frame_keys:
        frame_keys = sorted(set(original) | set(repaired), key=_frame_sort_key)

    rows = []
    improved_count = 0
    regressed_count = 0
    missing_count = 0
    for key in frame_keys:
        delta = _support_delta(original.get(key), repaired.get(key))
        observation_gain = int(delta["delta_observation_count"])
        ratio_gain = delta["delta_valid_observation_ratio"]
        improved = observation_gain >= min_observation_gain and (
            ratio_gain is None or ratio_gain >= min_ratio_gain
        )
        regressed = observation_gain < 0 or (ratio_gain is not None and ratio_gain < 0)
        if improved:
            improved_count += 1
            decision = "support_improved"
        elif regressed:
            regressed_count += 1
            decision = "support_regressed"
        elif not delta["repaired_registered"]:
            missing_count += 1
            decision = "support_missing"
        else:
            decision = "support_held"
        rows.append({
            "frame": key,
            **delta,
            "decision": decision,
        })

    if regressed_count or missing_count:
        decision = "hold"
    elif rows and (improved_count == len(rows) or (improved_count > 0 and not require_all_improved)):
        decision = "proceed_to_training_probe"
    else:
        decision = "hold"

    summary = {
        "schema": "capture_splat.colmap_support_delta.v0.1",
        "decision": decision,
        "authority": {
            "support_evidence_only": True,
            "training_result": False,
            "quality_claim": False,
        },
        "original_images": str(original_images),
        "repaired_images": str(repaired_images),
        "weak_report": str(weak_report.resolve()) if weak_report else None,
        "thresholds": {
            "min_observation_gain": min_observation_gain,
            "min_ratio_gain": min_ratio_gain,
            "require_all_improved": require_all_improved,
        },
        "frame_count": len(rows),
        "improved_count": improved_count,
        "regressed_count": regressed_count,
        "missing_count": missing_count,
        "frames": rows,
    }
    write_json_strict(out_dir / "capture_splat_colmap_support_delta.json", summary)
    return summary


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare per-frame COLMAP observation support before and after a repair pass.")
    parser.add_argument("--original-images", type=Path, required=True)
    parser.add_argument("--repaired-images", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--frames")
    parser.add_argument("--weak-report", type=Path)
    parser.add_argument("--min-observation-gain", type=int, default=100)
    parser.add_argument("--min-ratio-gain", type=float, default=0.03)
    parser.add_argument("--require-all-improved", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    summary = compare_colmap_support_delta(
        args.original_images,
        args.repaired_images,
        args.out,
        frames=args.frames,
        weak_report=args.weak_report,
        min_observation_gain=args.min_observation_gain,
        min_ratio_gain=args.min_ratio_gain,
        require_all_improved=args.require_all_improved,
    )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
