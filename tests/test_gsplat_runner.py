from pathlib import Path

import pytest

from capture_splat.gsplat_runner import run_gsplat
from capture_splat.json_utils import write_json_strict


def make_package(root: Path) -> Path:
    package = root / "package"
    (package / "images").mkdir(parents=True)
    sparse = package / "sparse" / "0"
    sparse.mkdir(parents=True)
    (package / "images" / "000001.jpg").write_bytes(b"fixture")
    write_json_strict(package / "capture.json", {"source": "capture_splat.prepare_capture"})
    (sparse / "cameras.txt").write_text("# cameras\n", encoding="utf-8")
    (sparse / "images.txt").write_text("# images\n", encoding="utf-8")
    return package


def make_gsplat_root(root: Path, support_masks: bool = False) -> Path:
    gsplat = root / "gsplat"
    examples = gsplat / "examples"
    examples.mkdir(parents=True)
    (examples / "simple_trainer.py").write_text(
        "post_processing: str | None = None\n"
        "steps_scaler = 1.0\nrandom_bkgd = False\ncap_max = 1000000\n"
        f"print('--post-processing {{None,bilateral_grid,ppisp}} --random-bkgd --steps-scaler --strategy.cap-max{' --mask-dir' if support_masks else ''}')\n",
        encoding="utf-8",
    )
    return gsplat


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
