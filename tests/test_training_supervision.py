from pathlib import Path

import numpy as np
import pytest

from capture_splat.json_utils import load_json_strict, write_json_strict
from capture_splat.training_supervision import (
    copy_capture_supervision_assets,
    prepare_training_supervision,
    resolve_supervision_policy,
    supervision_evidence,
)


def make_package(root: Path, *, include_second_without_depth: bool = False) -> Path:
    package = root / "package"
    (package / "images").mkdir(parents=True)
    (package / "depth").mkdir()
    (package / "confidence").mkdir()
    (package / "images/000001.jpg").write_bytes(b"image")
    depth = np.full((6, 8), 1.25, dtype=np.float32)
    depth[0, 0] = np.nan
    np.save(package / "depth/000001.npy", depth, allow_pickle=False)
    confidence = np.full((6, 8), 2, dtype=np.uint8)
    confidence[0, 1] = 0
    np.save(package / "confidence/000001.npy", confidence, allow_pickle=False)
    frames = [{
        "rgb": "images/000001.jpg",
        "depth": "depth/000001.npy",
        "confidence": "confidence/000001.npy",
        "intrinsics": {"fl_x": 8, "fl_y": 8, "cx": 4, "cy": 3, "w": 8, "h": 6},
    }]
    if include_second_without_depth:
        (package / "images/000002.jpg").write_bytes(b"image")
        frames.append({
            "rgb": "images/000002.jpg",
            "intrinsics": {"fl_x": 8, "fl_y": 8, "cx": 4, "cy": 3, "w": 8, "h": 6},
        })
    write_json_strict(package / "capture.json", {
        "schema": "capture_splat.v0.3",
        "source": "capture_splat.prepare_capture",
        "depth_scale": 1.0,
        "frames": frames,
    })
    return package


def test_prepare_training_supervision_validates_depth_and_derives_normals(tmp_path: Path) -> None:
    package = make_package(tmp_path)

    report = prepare_training_supervision(package)

    assert report["decision"] == "ready"
    assert report["validated_depth_count"] == 1
    assert report["confidence_filtered_depth_count"] == 1
    assert report["derived_normal_count"] == 1
    record = report["records"][0]
    assert record["status"] == "validated"
    assert record["valid_fraction"] == pytest.approx(46 / 48)
    normals = np.load(package / record["normal"], allow_pickle=False)
    assert normals.shape == (6, 8, 3)
    assert np.isfinite(normals).all()
    assert load_json_strict(package / "metadata/training_supervision.json")["schema"].endswith("v0.1")
    assert supervision_evidence(package)["valid"] is True


def test_partial_depth_coverage_is_held_not_rejected(tmp_path: Path) -> None:
    package = make_package(tmp_path, include_second_without_depth=True)

    report = prepare_training_supervision(package, derive_normals=False)

    assert report["decision"] == "hold"
    assert report["complete_depth_coverage"] is False
    assert "partial_metric_depth_coverage" in report["warnings"]


def test_supervision_checksum_tamper_is_rejected(tmp_path: Path) -> None:
    package = make_package(tmp_path)
    prepare_training_supervision(package)
    np.save(package / "depth/000001.npy", np.ones((6, 8), dtype=np.float32), allow_pickle=False)

    with pytest.raises(ValueError, match="checksum mismatch"):
        supervision_evidence(package)


def test_copy_capture_supervision_assets_preserves_relative_paths(tmp_path: Path) -> None:
    source = make_package(tmp_path / "source")
    target = tmp_path / "target"
    target.mkdir()
    capture = load_json_strict(source / "capture.json")

    result = copy_capture_supervision_assets(source, target, capture)

    assert result["complete"] is True
    assert result["copied"] == 2
    assert (target / "depth/000001.npy").is_file()
    assert (target / "confidence/000001.npy").is_file()


def test_copy_capture_supervision_rejects_escape(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    target = tmp_path / "target"
    target.mkdir()

    with pytest.raises(ValueError, match="escapes package"):
        copy_capture_supervision_assets(
            source,
            target,
            {"frames": [{"depth": "../outside.npy"}]},
        )


def test_required_supervision_blocks_unsupported_trainer(tmp_path: Path) -> None:
    package = make_package(tmp_path)
    prepare_training_supervision(package)

    with pytest.raises(RuntimeError, match="does not expose dedicated sensor depth"):
        resolve_supervision_policy(package, "required", "depth", None)

    state = resolve_supervision_policy(
        package,
        "required",
        "depth",
        "--sensor-depth-manifest",
    )
    assert state["applied"] is True
    assert state["semantics"] == "metric_sensor_depth_with_confidence_coverage_report"
