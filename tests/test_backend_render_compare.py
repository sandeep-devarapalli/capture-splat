from pathlib import Path

import pytest
from PIL import Image

from capture_splat.backend_render_compare import compare_backend_renders
from capture_splat.json_utils import load_json_strict, write_json_strict


def write_image(path: Path, color: tuple[int, int, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (8, 8), color).save(path)


def write_ascii_ply(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join([
            "ply",
            "format ascii 1.0",
            "element vertex 1",
            "property float x",
            "property float y",
            "property float z",
            "end_header",
            "0 0 0",
        ]) + "\n",
        encoding="ascii",
    )


def write_fixed_set(package: Path, frames: list[str]) -> None:
    write_json_strict(package / "metadata/fixed_camera_evaluation_set.json", {
        "schema": "capture_splat.fixed_camera_evaluation_set.v0.1",
        "frames": frames,
    })


def test_compare_backend_renders_records_renderer_missing(tmp_path: Path) -> None:
    package = tmp_path / "package"
    write_image(package / "images" / "000001.jpg", (10, 20, 30))
    write_fixed_set(package, ["000001.jpg"])

    summary = compare_backend_renders(
        package,
        tmp_path / "out",
        frames="000001",
    )
    loaded = load_json_strict(tmp_path / "out" / "capture_splat_backend_render_comparison.json")

    assert summary["decision"] == "hold"
    assert summary["frame_count"] == 1
    assert "renderer_missing" in loaded["backends"]["gsplat"]["warnings"]
    assert "renderer_missing" in loaded["backends"]["vksplat"]["warnings"]
    assert loaded["authority"]["quality_claim"] is False


def test_compare_backend_renders_runs_qa_for_existing_renders(tmp_path: Path) -> None:
    package = tmp_path / "package"
    gsplat = tmp_path / "renders" / "gsplat"
    vksplat = tmp_path / "renders" / "vksplat"
    write_image(package / "images" / "000001.jpg", (64, 96, 128))
    write_fixed_set(package, ["000001.jpg"])
    write_image(gsplat / "000001.jpg", (64, 96, 128))
    write_image(vksplat / "000001.jpg", (255, 255, 255))
    write_ascii_ply(tmp_path / "gsplat.ply")
    write_ascii_ply(tmp_path / "vksplat.ply")

    summary = compare_backend_renders(
        package,
        tmp_path / "out",
        frames="000001",
        gsplat_ply=tmp_path / "gsplat.ply",
        vksplat_ply=tmp_path / "vksplat.ply",
        gsplat_render_dir=gsplat,
        vksplat_render_dir=vksplat,
        gsplat_renderer_command="fixture gsplat render",
        vksplat_renderer_command="fixture vksplat render",
    )

    assert summary["backends"]["gsplat"]["decision"] == "promote"
    assert summary["backends"]["vksplat"]["decision"] == "hold"
    assert summary["metric_mean_deltas"]["psnr"] > 0
    assert summary["metric_mean_deltas"]["mae"] < 0
    assert (tmp_path / "out" / "qa" / "gsplat" / "capture_splat_render_source_qa_summary.json").exists()
    assert (tmp_path / "out" / "qa" / "vksplat" / "capture_splat_render_source_qa_summary.json").exists()


def test_compare_backend_renders_enforces_package_fixed_camera_set(tmp_path: Path) -> None:
    package = tmp_path / "package"
    write_image(package / "images" / "000001.jpg", (10, 20, 30))
    write_image(package / "images" / "000002.jpg", (20, 30, 40))
    write_fixed_set(package, ["000002.jpg"])

    summary = compare_backend_renders(package, tmp_path / "out")

    assert summary["frame_count"] == 1
    assert summary["authority"]["fixed_camera_evaluation_enforced"] is True
    with pytest.raises(ValueError, match="must match"):
        compare_backend_renders(package, tmp_path / "other", frames="000001")


def test_compare_backend_renders_blocks_without_fixed_camera_set(tmp_path: Path) -> None:
    package = tmp_path / "package"
    write_image(package / "images" / "000001.jpg", (10, 20, 30))

    with pytest.raises(FileNotFoundError, match="fixed-camera evaluation set missing"):
        compare_backend_renders(package, tmp_path / "out")
