from pathlib import Path

import pytest
from PIL import Image

from capture_splat.capture_quality_report import run_capture_quality_report
from capture_splat.json_utils import load_json_strict, write_json_strict


def make_quality_capture(
    root: Path,
    accepted: int,
    rejected: int = 0,
    blur: float = 0.02,
    parallax: float = 0.12,
    include_clipped_exposure: bool = True,
    include_feature_grid_coverage: bool = True,
) -> Path:
    image_dir = root / "rgb"
    metadata_dir = root / "metadata"
    image_dir.mkdir(parents=True)
    metadata_dir.mkdir()
    frames = []
    for index in range(accepted + rejected):
        accepted_frame = index < accepted
        name = f"{index + 1:06d}.jpg"
        Image.new("RGB", (4, 4), (index, 0, 0)).save(image_dir / name)
        capture_quality = {
            "accepted": accepted_frame,
            "reason": "useful_keyframe" if accepted_frame else "low_blur_score",
            "score": 0.9 if accepted_frame else 0.1,
            "blur_score": blur,
            "exposure_mean": 0.5,
            "exposure_delta": 0.02,
            "parallax_meters": parallax,
            "colmap_overlap_score": 0.8,
            "valid_depth_ratio": 0.7,
            "feature_point_count": 120,
        }
        if include_clipped_exposure:
            capture_quality["clipped_highlight_fraction"] = 0.03
            capture_quality["clipped_shadow_fraction"] = 0.01
        if include_feature_grid_coverage:
            capture_quality["feature_grid_coverage"] = 0.5
        frames.append({
            "rgb": f"rgb/{name}",
            "timestamp": float(index),
            "transform_matrix": [[1, 0, 0, index * 0.1], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]],
            "capture_quality": capture_quality,
        })
    write_json_strict(root / "capture.json", {
        "schema": "capture_splat.v0.1",
        "intrinsics": {"fl_x": 4.0, "fl_y": 4.0, "cx": 2.0, "cy": 2.0, "w": 4, "h": 4},
        "frames": frames,
    })
    write_json_strict(metadata_dir / "keyframe_report.json", {
        "schema": "capture_splat.keyframe_report.v0.1",
        "accepted_keyframes": accepted,
        "skipped_keyframe_candidates": rejected,
        "haptic_accepted_count": accepted,
        "skip_reason_counts": {"low_blur_score": rejected},
    })
    write_json_strict(metadata_dir / "room_capture_quality_report.json", {
        "schema": "capture_splat.room_quality_report.v0.1",
    })
    return root


def test_capture_quality_report_promotes_usable_capture(tmp_path: Path) -> None:
    capture = make_quality_capture(tmp_path / "capture", accepted=24, rejected=4)

    summary = run_capture_quality_report(capture, tmp_path / "out")
    loaded = load_json_strict(tmp_path / "out" / "capture_splat_capture_quality_report.json")

    assert summary["decision"] == "promote"
    assert summary["frame_selection"]["selected_frames"] == 24
    assert summary["metrics"]["clipped_highlight_fraction"]["mean"] == pytest.approx(0.03)
    assert summary["metrics"]["clipped_shadow_fraction"]["mean"] == pytest.approx(0.01)
    assert summary["metrics"]["feature_grid_coverage"]["mean"] == pytest.approx(0.5)
    assert loaded["metadata"]["haptic_accepted_count"] == 24


def test_capture_quality_report_holds_weak_capture(tmp_path: Path) -> None:
    capture = make_quality_capture(tmp_path / "capture", accepted=8, rejected=10, blur=0.001)

    summary = run_capture_quality_report(capture, tmp_path / "out")

    assert summary["decision"] == "hold"
    assert "accepted_frame_count_below_threshold" in summary["warnings"]
    assert "mean_blur_below_threshold" in summary["warnings"]


def test_capture_quality_report_rejects_no_accepted_frames(tmp_path: Path) -> None:
    capture = make_quality_capture(tmp_path / "capture", accepted=0, rejected=3)

    summary = run_capture_quality_report(capture, tmp_path / "out")

    assert summary["decision"] == "reject"
    assert summary["blockers"] == ["no_accepted_frames"]


def test_capture_quality_report_tolerates_missing_clipped_exposure_metrics(tmp_path: Path) -> None:
    capture = make_quality_capture(tmp_path / "capture", accepted=24, rejected=0, include_clipped_exposure=False)

    summary = run_capture_quality_report(capture, tmp_path / "out")

    assert summary["decision"] == "promote"
    assert summary["metrics"]["clipped_highlight_fraction"]["mean"] is None
    assert summary["metrics"]["clipped_shadow_fraction"]["mean"] is None


def test_capture_quality_report_tolerates_missing_feature_grid_coverage(tmp_path: Path) -> None:
    capture = make_quality_capture(tmp_path / "capture", accepted=24, rejected=0, include_feature_grid_coverage=False)

    summary = run_capture_quality_report(capture, tmp_path / "out")

    assert summary["decision"] == "promote"
    assert summary["metrics"]["feature_grid_coverage"]["mean"] is None
