from pathlib import Path

import pytest

from capture_splat.gsplat_runner import run_gsplat


def make_package(root: Path) -> Path:
    package = root / "package"
    (package / "images").mkdir(parents=True)
    sparse = package / "sparse" / "0"
    sparse.mkdir(parents=True)
    (package / "images" / "000001.jpg").write_bytes(b"fixture")
    (sparse / "cameras.txt").write_text("# cameras\n", encoding="utf-8")
    (sparse / "images.txt").write_text("# images\n", encoding="utf-8")
    return package


def make_gsplat_root(root: Path) -> Path:
    gsplat = root / "gsplat"
    examples = gsplat / "examples"
    examples.mkdir(parents=True)
    (examples / "simple_trainer.py").write_text("print('fake')\n", encoding="utf-8")
    return gsplat


def test_gsplat_dry_run_records_full_command(tmp_path: Path) -> None:
    package = make_package(tmp_path)
    gsplat = make_gsplat_root(tmp_path)

    summary = run_gsplat(package, tmp_path / "out", gsplat, steps=3000, strategy="mcmc", dry_run=True)

    assert summary["schema"] == "capture_splat.gsplat_run_summary.v0.1"
    assert summary["dry_run"] is True
    assert summary["steps"] == 3000
    assert "--disable_viewer" in summary["command"]
    assert "--disable_video" in summary["command"]
    assert "--save_ply" in summary["command"]
    assert "[3000]" in summary["command"]
    assert (tmp_path / "out" / "capture_splat_gsplat_summary.json").exists()


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
