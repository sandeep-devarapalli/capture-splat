from pathlib import Path

import pytest

from capture_splat.json_utils import write_json_strict
from capture_splat.scene_transform import _sha256
from capture_splat.training_supervision import prepare_training_supervision
from capture_splat.vksplat_ladder import run_vksplat_ladder
from capture_splat.vksplat_runner import run_vksplat


def make_package(root: Path) -> Path:
    package = root / "package"
    (package / "images").mkdir(parents=True)
    sparse = package / "sparse" / "0"
    sparse.mkdir(parents=True)
    (package / "images" / "000001.jpg").write_bytes(b"fixture")
    (sparse / "cameras.txt").write_text("# cameras\n", encoding="utf-8")
    (sparse / "images.txt").write_text("# images\n", encoding="utf-8")
    (sparse / "points3D.txt").write_text("# points\n", encoding="utf-8")
    return package


def make_vksplat_root(
    root: Path,
    *,
    support_sensor_depth: bool = False,
    support_sensor_normals: bool = False,
) -> Path:
    vksplat = root / "vksplat"
    vksplat.mkdir()
    (vksplat / "simple_trainer.py").write_text(
        "class MCMCTrainerConfig: mask_dir = None\n"
        "class TrainerConfig: mask_dir = None\n"
        + ("sensor_depth_manifest = None\n" if support_sensor_depth else "")
        + ("sensor_normal_manifest = None\n" if support_sensor_normals else "")
        + "def train_main(config): pass\n",
        encoding="utf-8",
    )
    return vksplat


def add_supervision(package: Path) -> None:
    import numpy as np

    (package / "depth").mkdir()
    (package / "confidence").mkdir()
    np.save(package / "depth/000001.npy", np.ones((3, 4), dtype=np.float32), allow_pickle=False)
    np.save(package / "confidence/000001.npy", np.full((3, 4), 2, dtype=np.uint8), allow_pickle=False)
    write_json_strict(package / "capture.json", {
        "source": "capture_splat.prepare_capture",
        "depth_scale": 1.0,
        "frames": [{
            "rgb": "images/000001.jpg",
            "depth": "depth/000001.npy",
            "confidence": "confidence/000001.npy",
            "intrinsics": {"fl_x": 4, "fl_y": 4, "cx": 2, "cy": 1.5, "w": 4, "h": 3},
        }],
    })
    prepare_training_supervision(package)


def write_qa(path: Path, psnr: float, ssim: float, mae: float, correlation: float, decision: str = "promote") -> None:
    write_json_strict(path, {
        "schema": "capture_splat.render_source_qa.v0.1",
        "decision": decision,
        "aggregates": {
            "psnr": {"mean": psnr, "min": psnr, "max": psnr},
            "ssim": {"mean": ssim, "min": ssim, "max": ssim},
            "mae": {"mean": mae, "min": mae, "max": mae},
            "normalized_correlation": {"mean": correlation, "min": correlation, "max": correlation},
        },
    })


def make_metric_package(package: Path) -> None:
    sparse = package / "sparse" / "0"
    metadata = package / "metadata"
    metadata.mkdir()
    write_json_strict(metadata / "metric_scale_report.json", {
        "schema": "capture_splat.metric_scale_report.v0.1",
        "status": "accepted",
        "target_units": "meters",
        "authority": {"metric_scale_evidence": True},
        "output_checksums": {
            "cameras_txt": _sha256(sparse / "cameras.txt"),
            "images_txt": _sha256(sparse / "images.txt"),
            "points3D_txt": _sha256(sparse / "points3D.txt"),
        },
    })


def test_ladder_dry_run_records_commands_and_rejects_regression(tmp_path: Path) -> None:
    package = make_package(tmp_path)
    vksplat = make_vksplat_root(tmp_path)
    qa = tmp_path / "qa"
    qa.mkdir()
    write_qa(qa / "step_0003000.json", 24.0, 0.95, 0.02, 0.95)
    write_qa(qa / "step_0007000.json", 20.0, 0.90, 0.04, 0.90)

    summary = run_vksplat_ladder(
        package,
        tmp_path / "out",
        vksplat,
        steps=[3000, 7000, 15000],
        qa_summary_dir=qa,
        dry_run=True,
        max_psnr_drop=0.5,
    )

    assert summary["decision"] == "reject"
    assert summary["stop_reason"] == "step_0007000_rejected"
    assert len(summary["rungs"]) == 2
    assert summary["rungs"][0]["command"]
    assert summary["vksplat_schedule"] == {"stop_reset_at": None}
    assert summary["rungs"][0]["run_summary"]["stop_reset_at"] is None
    assert summary["rungs"][0]["decision"] == "hold"
    assert "mean_psnr_regressed" in summary["rungs"][1]["reasons"]
    assert (tmp_path / "out" / "capture_splat_vksplat_ladder_summary.json").exists()


def test_ladder_can_use_sanitized_finite_ply_with_promoting_qa(tmp_path: Path, monkeypatch) -> None:
    package = make_package(tmp_path)
    vksplat = make_vksplat_root(tmp_path)
    qa = tmp_path / "qa"
    qa.mkdir()
    write_qa(qa / "step_0003000.json", 24.0, 0.95, 0.02, 0.95)

    def fake_run_vksplat(package_dir, output_root, vksplat_root, **kwargs):
        splat = output_root / "run" / "splat.ply"
        splat.parent.mkdir(parents=True)
        splat.write_text(
            "\n".join([
                "ply",
                "format ascii 1.0",
                "element vertex 3",
                "property float x",
                "property float y",
                "property float z",
                "property float scale_0",
                "property float scale_1",
                "property float scale_2",
                "end_header",
                "0 0 0 0 0 0",
                "nan 1 2 0.1 0.2 0.3",
                "1 2 3 0.4 0.5 0.6",
            ]) + "\n",
            encoding="ascii",
        )
        return {"command": ["fake"], "splat_ply": str(splat), "returncode": 0}

    monkeypatch.setattr("capture_splat.vksplat_ladder.run_vksplat", fake_run_vksplat)

    summary = run_vksplat_ladder(
        package,
        tmp_path / "out",
        vksplat,
        steps=[3000],
        qa_summary_dir=qa,
        sanitize_non_finite_ply=True,
    )

    rung = summary["rungs"][0]
    assert summary["decision"] == "promote"
    assert rung["decision"] == "promote"
    assert rung["finite_ply"] is True
    assert rung["original_ply_stats"]["finite"] is False
    assert rung["ply_stats"]["finite"] is True
    assert rung["ply_sanitize_report"]["dropped_vertex_count"] == 1
    assert "non_finite_ply_sanitized" in rung["reasons"]


def test_ladder_threads_stop_reset_schedule_into_runner(tmp_path: Path) -> None:
    package = make_package(tmp_path)
    vksplat = make_vksplat_root(tmp_path)

    summary = run_vksplat_ladder(
        package,
        tmp_path / "out",
        vksplat,
        steps=[15000],
        dry_run=True,
        stop_reset_at=9000,
    )

    rung = summary["rungs"][0]
    runner = tmp_path / "out" / "step_0015000" / "capture_splat_vksplat_runner.py"
    assert summary["vksplat_schedule"] == {"stop_reset_at": 9000}
    assert rung["run_summary"]["stop_reset_at"] == 9000
    assert "config.stop_reset_at = 9000" in runner.read_text(encoding="utf-8")


def test_ladder_threads_white_valid_masks_into_vksplat(tmp_path: Path) -> None:
    package = make_package(tmp_path)
    masks = package / "masks" / "valid"
    masks.mkdir(parents=True)
    (masks / "000001.jpg.png").write_bytes(b"fixture")
    vksplat = make_vksplat_root(tmp_path)

    summary = run_vksplat_ladder(
        package,
        tmp_path / "out",
        vksplat,
        steps=[3000],
        dry_run=True,
        masks="required",
    )

    runner = tmp_path / "out" / "step_0003000" / "capture_splat_vksplat_runner.py"
    assert summary["rungs"][0]["run_summary"]["masks"]["applied"] is True
    assert "config.mask_dir = 'masks/valid'" in runner.read_text(encoding="utf-8")


def test_vksplat_required_masks_block_incomplete_frame_coverage(tmp_path: Path) -> None:
    package = make_package(tmp_path)
    (package / "images/000002.tiff").write_bytes(b"fixture")
    masks = package / "masks" / "valid"
    masks.mkdir(parents=True)
    (masks / "000001.jpg.png").write_bytes(b"fixture")
    vksplat = make_vksplat_root(tmp_path)

    with pytest.raises(RuntimeError, match="required masks"):
        run_vksplat(package, tmp_path / "out", vksplat, masks="required", dry_run=True)


def test_vksplat_required_sensor_manifests_are_written_only_when_supported(tmp_path: Path) -> None:
    package = make_package(tmp_path)
    add_supervision(package)
    vksplat = make_vksplat_root(
        tmp_path,
        support_sensor_depth=True,
        support_sensor_normals=True,
    )

    summary = run_vksplat(
        package,
        tmp_path / "out",
        vksplat,
        depth_supervision="required",
        normal_supervision="required",
        dry_run=True,
    )

    runner = (tmp_path / "out/capture_splat_vksplat_runner.py").read_text(encoding="utf-8")
    report = str(package / "metadata/training_supervision.json")
    assert f"config.sensor_depth_manifest = {report!r}" in runner
    assert f"config.sensor_normal_manifest = {report!r}" in runner
    assert summary["sensor_supervision"]["depth"]["applied"] is True


def test_vksplat_required_sensor_depth_blocks_unsupported_trainer(tmp_path: Path) -> None:
    package = make_package(tmp_path)
    add_supervision(package)
    vksplat = make_vksplat_root(tmp_path)

    with pytest.raises(RuntimeError, match="dedicated sensor depth"):
        run_vksplat(
            package,
            tmp_path / "out",
            vksplat,
            depth_supervision="required",
            dry_run=True,
        )


def test_vksplat_auto_records_metric_normalization_limitation(tmp_path: Path) -> None:
    package = make_package(tmp_path)
    make_metric_package(package)
    vksplat = make_vksplat_root(tmp_path)

    summary = run_vksplat(package, tmp_path / "out", vksplat, dry_run=True)

    assert summary["normalization"]["resolved"] == "on"
    assert summary["normalization"]["metric_package"]["accepted"] is True
    assert summary["normalization"]["warning"] == "metric_package_normalized_backend_cannot_disable"


def test_vksplat_normalization_off_rejects_unsupported_backend(tmp_path: Path) -> None:
    package = make_package(tmp_path)
    make_metric_package(package)
    vksplat = make_vksplat_root(tmp_path)

    with pytest.raises(RuntimeError, match="real normalization-disable capability"):
        run_vksplat(package, tmp_path / "out", vksplat, normalization="off", dry_run=True)
