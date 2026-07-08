from pathlib import Path

from PIL import Image

from capture_splat.json_utils import load_json_strict, write_json_strict
from capture_splat.weak_frames_report import run_weak_frames_report


def write_image(path: Path, color: tuple[int, int, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (8, 8), color).save(path)


def test_weak_frames_report_attaches_colmap_capture_and_contact_sheet(tmp_path: Path) -> None:
    source = tmp_path / "source" / "000001.jpg"
    render = tmp_path / "render" / "val_00000.png"
    write_image(source, (10, 20, 30))
    write_image(render, (12, 18, 28))
    qa = tmp_path / "qa.json"
    write_json_strict(qa, {
        "schema": "capture_splat.render_source_qa.v0.1",
        "weak_frames": ["000001"],
        "tail_frames": ["000001"],
        "frames": [{
            "frame_id": "000001",
            "source": str(source),
            "render": str(render),
            "psnr": 15.0,
            "ssim": 0.72,
            "mae": 0.1,
            "normalized_correlation": 0.7,
            "source_edge_density": 0.01,
            "render_edge_density": 0.002,
            "source_laplacian_variance": 0.002,
            "render_laplacian_variance": 0.0001,
            "weak_reasons": ["psnr_below_threshold"],
        }],
    })
    colmap_images = tmp_path / "images.txt"
    colmap_images.write_text(
        "\n".join([
            "# Image list with two lines of data per image:",
            "1 1 0 0 0 0 0 0 1 000001.jpg",
            "0 0 42 1 1 -1 2 2 -1",
        ]) + "\n",
        encoding="utf-8",
    )
    capture = tmp_path / "capture"
    capture.mkdir()
    write_json_strict(capture / "capture.json", {
        "frames": [{
            "rgb": "rgb/frame_000001.jpg",
            "capture_quality": {
                "blur_score": 0.001,
                "parallax_meters": 0.01,
                "colmap_overlap_score": 0.2,
                "clipped_highlight_fraction": 0.03,
            },
        }],
    })

    summary = run_weak_frames_report(
        qa,
        tmp_path / "out",
        colmap_images=colmap_images,
        capture=capture,
        min_colmap_observations=2,
    )
    loaded = load_json_strict(tmp_path / "out" / "capture_splat_weak_frames_report.json")
    frame = loaded["frames"][0]

    assert summary["decision"] == "hold"
    assert frame["colmap_support"]["observation_count"] == 1
    assert frame["capture_quality"]["blur_score"] == 0.001
    assert "weak_colmap_support" in frame["possible_reason_buckets"]
    assert "render_sharpness_below_source" in frame["possible_reason_buckets"]
    assert "capture_blur_proxy_low" in frame["possible_reason_buckets"]
    assert Path(loaded["contact_sheet"]).exists()


def test_weak_frames_report_flags_low_colmap_observation_ratio(tmp_path: Path) -> None:
    source = tmp_path / "source" / "000002.jpg"
    render = tmp_path / "render" / "val_00001.png"
    write_image(source, (20, 30, 40))
    write_image(render, (22, 28, 38))
    qa = tmp_path / "qa.json"
    write_json_strict(qa, {
        "schema": "capture_splat.render_source_qa.v0.1",
        "weak_frames": ["000002"],
        "tail_frames": [],
        "frames": [{
            "frame_id": "000002",
            "source": str(source),
            "render": str(render),
            "psnr": 19.0,
            "ssim": 0.88,
            "mae": 0.09,
            "normalized_correlation": 0.8,
            "source_edge_density": 0.01,
            "render_edge_density": 0.009,
            "source_laplacian_variance": 0.002,
            "render_laplacian_variance": 0.001,
            "weak_reasons": ["psnr_below_threshold"],
        }],
    })
    colmap_images = tmp_path / "images.txt"
    colmap_images.write_text(
        "\n".join([
            "# Image list with two lines of data per image:",
            "2 1 0 0 0 0 0 0 1 000002.jpg",
            "0 0 42 1 1 -1 2 2 -1",
        ]) + "\n",
        encoding="utf-8",
    )

    summary = run_weak_frames_report(
        qa,
        tmp_path / "out",
        colmap_images=colmap_images,
        min_colmap_observations=1,
        min_colmap_observation_ratio=0.5,
    )

    frame = summary["frames"][0]
    assert frame["colmap_support"]["observation_count"] == 1
    assert frame["colmap_support"]["valid_observation_ratio"] == 1 / 3
    assert "weak_colmap_support" in frame["possible_reason_buckets"]
