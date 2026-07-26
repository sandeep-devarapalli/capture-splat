from pathlib import Path

import pytest

from capture_splat.gsplat_runner import run_gsplat
from capture_splat.json_utils import write_json_strict
from capture_splat.scene_transform import _sha256
from capture_splat.training_supervision import prepare_training_supervision


def make_package(root: Path) -> Path:
    package = root / "package"
    (package / "images").mkdir(parents=True)
    sparse = package / "sparse" / "0"
    sparse.mkdir(parents=True)
    (package / "images" / "000001.jpg").write_bytes(b"fixture")
    write_json_strict(package / "capture.json", {"source": "capture_splat.prepare_capture"})
    (sparse / "cameras.txt").write_text("# cameras\n", encoding="utf-8")
    (sparse / "images.txt").write_text("# images\n", encoding="utf-8")
    (sparse / "points3D.txt").write_text("# points\n", encoding="utf-8")
    return package


def make_gsplat_root(
    root: Path,
    support_masks: bool = False,
    support_sensor_depth: bool = False,
    support_sensor_normals: bool = False,
) -> Path:
    gsplat = root / "gsplat"
    examples = gsplat / "examples"
    examples.mkdir(parents=True)
    (examples / "simple_trainer.py").write_text(
        "post_processing: str | None = None\n"
        "normalize_world_space: bool = True\n"
        "steps_scaler = 1.0\nrandom_bkgd = False\ncap_max = 1000000\n"
        f"print('--post-processing {{None,bilateral_grid,ppisp}} --random-bkgd --steps-scaler --strategy.cap-max --strategy.refine-every{' --mask-dir' if support_masks else ''}{' --sensor-depth-manifest' if support_sensor_depth else ''}{' --sensor-normal-manifest' if support_sensor_normals else ''}')\n",
        encoding="utf-8",
    )
    return gsplat


def add_supervision(package: Path) -> None:
    import numpy as np

    (package / "depth").mkdir()
    (package / "confidence").mkdir()
    np.save(package / "depth/000001.npy", np.ones((3, 4), dtype=np.float32), allow_pickle=False)
    np.save(package / "confidence/000001.npy", np.full((3, 4), 2, dtype=np.uint8), allow_pickle=False)
    capture = {
        "source": "capture_splat.prepare_capture",
        "depth_scale": 1.0,
        "frames": [{
            "rgb": "images/000001.jpg",
            "depth": "depth/000001.npy",
            "confidence": "confidence/000001.npy",
            "intrinsics": {"fl_x": 4, "fl_y": 4, "cx": 2, "cy": 1.5, "w": 4, "h": 3},
        }],
    }
    write_json_strict(package / "capture.json", capture)
    prepare_training_supervision(package)


def make_metric_package(package: Path) -> None:
    sparse = package / "sparse" / "0"
    metadata = package / "metadata"
    metadata.mkdir()
    write_json_strict(metadata / "metric_scale_report.json", {
        "schema": "capture_splat.metric_scale_report.v0.1",
        "status": "accepted",
        "target_units": "meters",
        "target_coordinate_frame": "metric_colmap_world",
        "meters_per_colmap_unit": 0.42,
        "authority": {"metric_scale_evidence": True},
        "output_checksums": {
            "cameras_txt": _sha256(sparse / "cameras.txt"),
            "images_txt": _sha256(sparse / "images.txt"),
            "points3D_txt": _sha256(sparse / "points3D.txt"),
        },
    })


def test_gsplat_dry_run_scales_schedule_instead_of_truncating(tmp_path: Path) -> None:
    package = make_package(tmp_path)
    gsplat = make_gsplat_root(tmp_path)

    summary = run_gsplat(package, tmp_path / "out", gsplat, steps=3000, strategy="mcmc", dry_run=True)

    assert summary["schema"] == "capture_splat.gsplat_run_summary.v0.1"
    assert summary["dry_run"] is True
    assert summary["steps"] == 3000
    command = summary["command"]
    assert "--disable_viewer" in command
    assert "--disable_video" in command
    assert "--save_ply" in command
    assert command.count("30000") == 4
    assert "3000" not in command
    assert command[command.index("--steps_scaler") + 1] == "0.1"
    assert command[command.index("--post-processing") + 1] == "bilateral_grid"
    assert "--random_bkgd" in command
    assert command[command.index("--strategy.cap-max") + 1] == "1000000"
    assert command[command.index("--strategy.refine-every") + 1] == "2000"
    assert summary["mcmc_refine_every"]["target_effective_steps"] == 200
    assert summary["mcmc_refine_every"]["expected_effective_steps"] == 200
    assert (tmp_path / "out" / "capture_splat_gsplat_summary.json").exists()


def test_gsplat_full_schedule_run_omits_scaler_and_respects_opt_outs(tmp_path: Path) -> None:
    package = make_package(tmp_path)
    gsplat = make_gsplat_root(tmp_path)

    summary = run_gsplat(
        package,
        tmp_path / "out",
        gsplat,
        steps=30000,
        strategy="mcmc",
        dry_run=True,
        use_bilateral_grid=False,
        random_bkgd=False,
        max_gaussians=500_000,
    )

    command = summary["command"]
    assert "--steps_scaler" not in command
    assert command.count("30000") == 4
    assert "--post-processing" not in command
    assert "--random_bkgd" not in command
    assert command[command.index("--strategy.cap-max") + 1] == "500000"
    assert command[command.index("--strategy.refine-every") + 1] == "200"


def test_gsplat_auto_refine_cadence_uses_frame_count_and_compensates_scaler(tmp_path: Path) -> None:
    package = make_package(tmp_path)
    for index in range(2, 361):
        (package / "images" / f"{index:06d}.jpg").write_bytes(b"fixture")
    gsplat = make_gsplat_root(tmp_path)

    summary = run_gsplat(package, tmp_path / "out", gsplat, steps=7000, dry_run=True)

    cadence = summary["mcmc_refine_every"]
    assert cadence["frame_count"] == 360
    assert cadence["target_effective_steps"] == 400
    assert cadence["expected_effective_steps"] >= 400
    assert summary["command"][summary["command"].index("--strategy.refine-every") + 1] == str(
        cadence["trainer_command_value"]
    )


def test_gsplat_explicit_refine_cadence_requires_mcmc(tmp_path: Path) -> None:
    package = make_package(tmp_path)
    gsplat = make_gsplat_root(tmp_path)

    with pytest.raises(ValueError, match="only applies"):
        run_gsplat(
            package,
            tmp_path / "out",
            gsplat,
            strategy="default",
            mcmc_refine_every=400,
            dry_run=True,
        )


def test_gsplat_missing_package_is_rejected(tmp_path: Path) -> None:
    gsplat = make_gsplat_root(tmp_path)

    with pytest.raises(FileNotFoundError, match="image directory missing"):
        run_gsplat(tmp_path / "missing", tmp_path / "out", gsplat, dry_run=True)


def test_gsplat_missing_trainer_is_rejected(tmp_path: Path) -> None:
    package = make_package(tmp_path)
    gsplat = tmp_path / "gsplat"
    gsplat.mkdir()

    with pytest.raises(FileNotFoundError, match="simple_trainer.py"):
        run_gsplat(package, tmp_path / "out", gsplat, dry_run=True)


def test_gsplat_ppisp_requires_mcmc(tmp_path: Path) -> None:
    package = make_package(tmp_path)
    gsplat = make_gsplat_root(tmp_path)

    with pytest.raises(RuntimeError, match="requires the mcmc strategy"):
        run_gsplat(package, tmp_path / "out", gsplat, strategy="default", photometric="ppisp", dry_run=True)


def test_gsplat_required_masks_block_when_trainer_cannot_consume_them(tmp_path: Path) -> None:
    package = make_package(tmp_path)
    gsplat = make_gsplat_root(tmp_path)
    masks = package / "masks" / "valid"
    masks.mkdir(parents=True)
    (masks / "000001.jpg.png").write_bytes(b"fixture")

    with pytest.raises(RuntimeError, match="required masks"):
        run_gsplat(package, tmp_path / "out", gsplat, masks="required", dry_run=True)


def test_gsplat_passes_complete_required_masks_to_supported_trainer(tmp_path: Path) -> None:
    package = make_package(tmp_path)
    gsplat = make_gsplat_root(tmp_path, support_masks=True)
    masks = package / "masks" / "valid"
    masks.mkdir(parents=True)
    (masks / "000001.jpg.png").write_bytes(b"fixture")

    summary = run_gsplat(package, tmp_path / "out", gsplat, masks="required", dry_run=True)

    assert summary["masks"]["applied"] is True
    assert summary["command"][summary["command"].index("--mask-dir") + 1] == str(masks)


def test_gsplat_generic_package_defaults_to_no_photometric_correction(tmp_path: Path) -> None:
    package = make_package(tmp_path)
    (package / "capture.json").unlink()
    gsplat = make_gsplat_root(tmp_path)

    summary = run_gsplat(package, tmp_path / "out", gsplat, dry_run=True)

    assert summary["photometric"] == "none"
    assert "--post-processing" not in summary["command"]


def test_gsplat_required_masks_block_incomplete_frame_coverage(tmp_path: Path) -> None:
    package = make_package(tmp_path)
    (package / "images/000002.tiff").write_bytes(b"fixture")
    gsplat = make_gsplat_root(tmp_path, support_masks=True)
    masks = package / "masks" / "valid"
    masks.mkdir(parents=True)
    (masks / "000001.jpg.png").write_bytes(b"fixture")

    with pytest.raises(RuntimeError, match="required masks"):
        run_gsplat(package, tmp_path / "out", gsplat, masks="required", dry_run=True)


def test_gsplat_auto_preserves_unsupported_sensor_depth_evidence(tmp_path: Path) -> None:
    package = make_package(tmp_path)
    add_supervision(package)
    gsplat = make_gsplat_root(tmp_path)

    summary = run_gsplat(package, tmp_path / "out", gsplat, dry_run=True)

    depth = summary["sensor_supervision"]["depth"]
    assert depth["available"] is True
    assert depth["supported"] is False
    assert depth["applied"] is False
    assert depth["warning"] == "sensor_depth_evidence_preserved_but_trainer_unsupported"
    assert summary["trainer_capabilities"]["builtin_depth_loss"]["semantics"] == "colmap_sparse_point_depth_not_sensor_depth"
    assert "--depth-loss" not in summary["command"]


def test_gsplat_required_sensor_manifests_are_passed_only_when_supported(tmp_path: Path) -> None:
    package = make_package(tmp_path)
    add_supervision(package)
    gsplat = make_gsplat_root(
        tmp_path,
        support_sensor_depth=True,
        support_sensor_normals=True,
    )

    summary = run_gsplat(
        package,
        tmp_path / "out",
        gsplat,
        depth_supervision="required",
        normal_supervision="required",
        dry_run=True,
    )

    command = summary["command"]
    report = str(package / "metadata/training_supervision.json")
    assert command[command.index("--sensor-depth-manifest") + 1] == report
    assert command[command.index("--sensor-normal-manifest") + 1] == report


def test_gsplat_required_sensor_depth_blocks_unsupported_trainer(tmp_path: Path) -> None:
    package = make_package(tmp_path)
    add_supervision(package)
    gsplat = make_gsplat_root(tmp_path)

    with pytest.raises(RuntimeError, match="dedicated sensor depth"):
        run_gsplat(
            package,
            tmp_path / "out",
            gsplat,
            depth_supervision="required",
            dry_run=True,
        )


def test_gsplat_auto_disables_normalization_for_checksum_bound_metric_package(tmp_path: Path) -> None:
    package = make_package(tmp_path)
    make_metric_package(package)
    gsplat = make_gsplat_root(tmp_path)

    summary = run_gsplat(package, tmp_path / "out", gsplat, dry_run=True)

    assert summary["normalization"]["resolved"] == "off"
    assert summary["normalization"]["metric_package"]["accepted"] is True
    assert "--no-normalize-world-space" in summary["command"]


def test_gsplat_normalization_off_requires_accepted_metric_package(tmp_path: Path) -> None:
    package = make_package(tmp_path)
    gsplat = make_gsplat_root(tmp_path)

    with pytest.raises(RuntimeError, match="accepted metric package"):
        run_gsplat(package, tmp_path / "out", gsplat, normalization="off", dry_run=True)


def test_gsplat_auto_rejects_stale_metric_binding_and_normalizes(tmp_path: Path) -> None:
    package = make_package(tmp_path)
    make_metric_package(package)
    (package / "sparse/0/images.txt").write_text("# changed\n", encoding="utf-8")
    gsplat = make_gsplat_root(tmp_path)

    summary = run_gsplat(package, tmp_path / "out", gsplat, dry_run=True)

    assert summary["normalization"]["resolved"] == "on"
    assert summary["normalization"]["metric_package"]["accepted"] is False
    assert "checksum_mismatch:images_txt" in summary["normalization"]["metric_package"]["reason"]
    assert "--no-normalize-world-space" not in summary["command"]
