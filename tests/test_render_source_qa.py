import math
from pathlib import Path

from PIL import Image

from capture_splat.json_utils import load_json_strict
from capture_splat.render_source_qa import run_render_source_qa


def write_image(path: Path, color: tuple[int, int, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (8, 8), color).save(path)


def test_identical_images_promote_with_finite_metrics(tmp_path: Path) -> None:
    source = tmp_path / "source"
    render = tmp_path / "render"
    write_image(source / "000001.png", (100, 120, 140))
    write_image(render / "000001.png", (100, 120, 140))

    summary = run_render_source_qa(source, render, tmp_path / "qa")
    loaded = load_json_strict(tmp_path / "qa" / "capture_splat_render_source_qa_summary.json")

    assert summary["decision"] == "promote"
    assert loaded["frames"][0]["psnr"] == 99.0
    assert all(math.isfinite(loaded["frames"][0][name]) for name in ("mae", "rmse", "ssim", "normalized_correlation"))


def test_degraded_image_holds(tmp_path: Path) -> None:
    source = tmp_path / "source"
    render = tmp_path / "render"
    write_image(source / "000001.png", (0, 0, 0))
    write_image(render / "000001.png", (255, 255, 255))

    summary = run_render_source_qa(source, render, tmp_path / "qa")

    assert summary["decision"] == "hold"
    assert summary["weak_frames"] == ["000001"]


def test_missing_and_dimension_mismatch_reject(tmp_path: Path) -> None:
    source = tmp_path / "source"
    render = tmp_path / "render"
    write_image(source / "000001.png", (10, 10, 10))
    render.mkdir()

    missing = run_render_source_qa(source, render, tmp_path / "missing")
    assert missing["decision"] == "reject"

    Image.new("RGB", (8, 8), (10, 10, 10)).save(source / "000002.png")
    Image.new("RGB", (4, 4), (10, 10, 10)).save(render / "000002.png")
    mismatch = run_render_source_qa(source, render, tmp_path / "mismatch")
    assert mismatch["decision"] == "reject"
    assert any("dimension mismatch" in error for error in mismatch["errors"])
