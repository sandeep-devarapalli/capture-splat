from pathlib import Path

from PIL import Image

from capture_splat.colmap_support_repair import build_colmap_support_repair
from capture_splat.json_utils import load_json_strict, write_json_strict


def write_image(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (8, 8), (20, 30, 40)).save(path)


def test_colmap_support_repair_writes_manifest_lists_and_pairs(tmp_path: Path) -> None:
    package = tmp_path / "package"
    for index in range(1, 9):
        write_image(package / "images" / f"{index:06d}.jpg")
    sparse = package / "sparse" / "0"
    sparse.mkdir(parents=True)
    sparse.joinpath("images.txt").write_text(
        "\n".join([
            "# images",
            "1 1 0 0 0 0 0 0 1 000001.jpg",
            "0 0 1 1 1 2 2 2 3",
            "2 1 0 0 0 0 0 0 1 000004.jpg",
            "0 0 1 1 1 -1 2 2 -1",
            "3 1 0 0 0 0 0 0 1 000008.jpg",
            "0 0 1 1 1 2 2 2 3",
        ]) + "\n",
        encoding="utf-8",
    )
    weak_report = tmp_path / "weak.json"
    write_json_strict(weak_report, {
        "schema": "capture_splat.weak_frames_report.v0.1",
        "frames": [{
            "frame_id": "000004",
            "qa_metrics": {"weak_reasons": ["psnr_below_threshold"]},
            "possible_reason_buckets": ["weak_colmap_support", "render_sharpness_below_source"],
        }],
    })
    capture = tmp_path / "capture"
    write_json_strict(capture / "capture.json", {
        "schema": "capture_splat.v0.1",
        "frames": [{
            "rgb": "rgb/frame_000004.jpg",
            "capture_quality": {"blur_score": 0.01, "parallax_meters": 0.2},
        }],
    })

    summary = build_colmap_support_repair(
        weak_report,
        package,
        tmp_path / "out",
        capture=capture,
        neighbor_radius=1,
        max_anchors_per_target=2,
        min_colmap_observations=2,
        min_colmap_observation_ratio=0.5,
    )
    manifest = load_json_strict(tmp_path / "out" / "capture_splat_colmap_support_repair_manifest.json")
    image_list = (tmp_path / "out" / "repair_image_list.txt").read_text(encoding="utf-8").splitlines()
    pairs = (tmp_path / "out" / "repair_pairs.txt").read_text(encoding="utf-8").splitlines()

    assert summary["decision"] == "hold"
    assert manifest["authority"]["colmap_repair_complete"] is False
    assert manifest["target_count"] == 1
    assert manifest["targets"][0]["priority"] == "high"
    assert manifest["targets"][0]["neighbor_images"] == ["000003.jpg", "000004.jpg", "000005.jpg"]
    assert manifest["targets"][0]["anchor_images"] == ["000001.jpg", "000008.jpg"]
    assert image_list == ["000001.jpg", "000003.jpg", "000004.jpg", "000005.jpg", "000008.jpg"]
    assert "000001.jpg 000004.jpg" in pairs
    assert "000004.jpg 000008.jpg" in pairs
