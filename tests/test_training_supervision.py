import hashlib
from pathlib import Path

import numpy as np
import pytest

from capture_splat.json_utils import load_json_strict, write_json_strict
from capture_splat.training_supervision import (
    copy_capture_manifest_assets,
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


def test_copy_capture_supervision_assets_preserves_relative_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_package(tmp_path / "source")
    target = tmp_path / "target"
    target.mkdir()
    capture = load_json_strict(source / "capture.json")
    capture["planes_file"] = "metadata/planes.json"
    (source / "metadata").mkdir()
    (source / "metadata/planes.json").write_text("{}\n", encoding="utf-8")

    result = copy_capture_supervision_assets(source, target, capture)

    assert result["complete"] is True
    assert result["copied"] == 2
    assert (target / "depth/000001.npy").is_file()
    assert (target / "confidence/000001.npy").is_file()

    (target / "images").mkdir()
    (target / "images/000001.jpg").write_bytes(b"generated")
    assets = copy_capture_manifest_assets(source, target, capture)
    assert assets["complete"] is False
    assert assets["conflicts"] == ["images/000001.jpg"]
    assert (target / "images/000001.jpg").read_bytes() == b"generated"
    assert (target / "metadata/planes.json").is_file()
    assert hashlib.sha256((source / "metadata/planes.json").read_bytes()).digest() == hashlib.sha256(
        (target / "metadata/planes.json").read_bytes()
    ).digest()

    missing = copy_capture_manifest_assets(source, tmp_path / "missing", {
        "planes_file": "metadata/missing.json",
        "frames": [{"object_mask": "masks/missing.png"}],
    })
    assert missing["decision"] == "hold"
    assert set(missing["missing"]) == {"metadata/missing.json", "masks/missing.png"}

    monkeypatch.setattr(
        "capture_splat.training_supervision.shutil.copy2",
        lambda _source, destination: Path(destination).write_bytes(b"corrupt"),
    )
    corrupt = copy_capture_manifest_assets(source, tmp_path / "corrupt", {
        "planes_file": "metadata/planes.json", "frames": []})
    assert corrupt["decision"] == "hold"
    assert corrupt["conflicts"] == ["metadata/planes.json"]
    assert corrupt["verified_asset_count"] == 0


def test_copy_capture_supervision_rejects_escape(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    target = tmp_path / "target"
    target.mkdir()

    with pytest.raises(ValueError, match="escapes package"):
        copy_capture_manifest_assets(
            source,
            target,
            {"planes_file": "../outside.npy", "frames": []},
        )
    for relative in (r"..\outside.npy", "C:/outside.npy", r"\\server\share\outside.npy"):
        with pytest.raises(ValueError, match="escapes package"):
            copy_capture_manifest_assets(
                source, target, {"planes_file": relative, "frames": []}
            )
    for relative in (
        "CON.json", "CON .txt", "COM1 .log", "COM¹.txt",
        "metadata/name.", "metadata/name ", "a?b", "file:stream",
    ):
        with pytest.raises(ValueError, match="not portable"):
            copy_capture_manifest_assets(
                source, target, {"planes_file": relative, "frames": []}
            )
    for frames in ({}, ["not-an-object"]):
        with pytest.raises(ValueError, match="capture frame"):
            copy_capture_manifest_assets(source, target, {"frames": frames})

    unicode_collision = copy_capture_manifest_assets(source, target, {
        "composed_file": "metadata/é.json",
        "decomposed_file": "metadata/e\N{COMBINING ACUTE ACCENT}.json",
        "frames": [],
    })
    assert set(unicode_collision["conflicts"]) == {
        "metadata/é.json", "metadata/e\N{COMBINING ACUTE ACCENT}.json",
    }

    (source / "metadata").mkdir()
    (source / "metadata/camera_evidence.json").write_text("{}\n", encoding="utf-8")
    (source / "sparse/0").mkdir(parents=True)
    (source / "sparse/0/images.txt").write_text("# model\n", encoding="utf-8")
    protected = copy_capture_manifest_assets(source, target, {
        "camera_evidence_file": "metadata/./camera_evidence.json",
        "duplicate_file": "metadata//camera_evidence.json",
        "case_file": "metadata/CAMERA_EVIDENCE.json",
        "sparse_file": "sparse//0/images.txt",
        "case_sparse_file": "Sparse/0/images.txt",
        "frames": [],
    }, protected={"metadata/camera_evidence.json", "sparse"})
    assert protected["reference_count"] == 5
    assert protected["unique_asset_count"] == 4
    assert set(protected["conflicts"]) == {
        "metadata/camera_evidence.json", "metadata/CAMERA_EVIDENCE.json",
        "sparse/0/images.txt", "Sparse/0/images.txt",
    }

    (source / "metadata/user.json").write_text("{}\n", encoding="utf-8")
    (target / "metadata").mkdir()
    (target / "metadata/user.json").symlink_to("camera_evidence.json")
    (source / "model_alias/0").mkdir(parents=True)
    (source / "model_alias/0/images.txt").write_text("# model\n", encoding="utf-8")
    (target / "model_alias").symlink_to("sparse", target_is_directory=True)
    aliases = copy_capture_manifest_assets(source, target, {
        "user_file": "metadata/user.json", "model_file": "model_alias/0/images.txt",
    }, protected={"metadata/camera_evidence.json", "sparse"})
    assert set(aliases["conflicts"]) == {"metadata/user.json", "model_alias/0/images.txt"}

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "asset.bin").write_bytes(b"outside")
    (source / "source_link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="escapes package"):
        copy_capture_manifest_assets(source, target, {"asset_file": "source_link/asset.bin"})

    (source / "target_link").mkdir()
    (source / "target_link/asset.bin").write_bytes(b"inside")
    (target / "target_link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="escapes package"):
        copy_capture_manifest_assets(source, target, {"asset_file": "target_link/asset.bin"})


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
