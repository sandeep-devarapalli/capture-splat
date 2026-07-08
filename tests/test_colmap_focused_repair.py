from pathlib import Path

from PIL import Image

from capture_splat.colmap_focused_repair import run_colmap_focused_repair, write_database_aligned_sparse_input
from capture_splat.colmap_support_repair import build_colmap_support_repair
from capture_splat.json_utils import load_json_strict, write_json_strict


def write_image(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (8, 8), (20, 30, 40)).save(path)


def write_package(package: Path) -> None:
    for name in ("000074.jpg", "000076.jpg", "000080.jpg", "000086.jpg"):
        write_image(package / "images" / name)
    sparse = package / "sparse" / "0"
    sparse.mkdir(parents=True)
    sparse.joinpath("cameras.txt").write_text(
        "\n".join([
            "# cameras",
            "1 PINHOLE 8 8 4 4 4 4",
        ]) + "\n",
        encoding="utf-8",
    )
    sparse.joinpath("images.txt").write_text(
        "\n".join([
            "# images",
            "10 1 0 0 0 1 2 3 1 000086.jpg",
            "0 0 11 1 1 12",
            "55 1 0 0 0 4 5 6 1 000076.jpg",
            "0 0 11 1 1 12",
            "80 1 0 0 0 7 8 9 1 000080.jpg",
            "0 0 11 1 1 12",
            "74 1 0 0 0 2 3 4 1 000074.jpg",
            "0 0 11 1 1 12",
        ]) + "\n",
        encoding="utf-8",
    )
    sparse.joinpath("points3D.txt").write_text(
        "\n".join([
            "# points",
            "11 0 0 0 255 255 255 0.1 10 0 55 0",
            "12 1 1 1 255 255 255 0.1 55 1 80 1",
        ]) + "\n",
        encoding="utf-8",
    )


def test_write_database_aligned_sparse_input_rewrites_image_and_frame_ids(tmp_path: Path) -> None:
    package = tmp_path / "package"
    write_package(package)

    summary = write_database_aligned_sparse_input(
        package,
        tmp_path / "sparse_input",
        {"000076.jpg": 2, "000086.jpg": 1},
        image_names=["000076.jpg", "000086.jpg"],
    )
    images = (tmp_path / "sparse_input" / "images.txt").read_text(encoding="utf-8")
    frames = (tmp_path / "sparse_input" / "frames.txt").read_text(encoding="utf-8")
    rigs = (tmp_path / "sparse_input" / "rigs.txt").read_text(encoding="utf-8")

    assert summary["image_count"] == 2
    assert "1 1 0 0 0 1 2 3 1 000086.jpg" in images
    assert "2 1 0 0 0 4 5 6 1 000076.jpg" in images
    assert "1 1 1 0 0 0 1 2 3 1 CAMERA 1 1" in frames
    assert "2 1 1 0 0 0 4 5 6 1 CAMERA 1 2" in frames
    assert "1 1 CAMERA 1" in rigs


def test_write_database_aligned_sparse_input_preserves_and_rewrites_points(tmp_path: Path) -> None:
    package = tmp_path / "package"
    write_package(package)

    summary = write_database_aligned_sparse_input(
        package,
        tmp_path / "sparse_input",
        {"000076.jpg": 2, "000086.jpg": 1, "000080.jpg": 3},
        image_names=["000076.jpg", "000086.jpg", "000080.jpg"],
        preserve_existing_points=True,
    )
    images = (tmp_path / "sparse_input" / "images.txt").read_text(encoding="utf-8")
    points = (tmp_path / "sparse_input" / "points3D.txt").read_text(encoding="utf-8")

    assert summary["preserve_existing_points"] is True
    assert "0 0 11 1 1 12" in images
    assert "11 0 0 0 255 255 255 0.1 1 0 2 0" in points
    assert "12 1 1 1 255 255 255 0.1 2 1 3 1" in points


def test_colmap_focused_repair_dry_run_uses_existing_repair_manifest(tmp_path: Path) -> None:
    package = tmp_path / "package"
    write_package(package)
    weak_report = tmp_path / "weak.json"
    write_json_strict(weak_report, {
        "schema": "capture_splat.weak_frames_report.v0.1",
        "frames": [{
            "frame_id": "000086",
            "qa_metrics": {"weak_reasons": ["psnr_below_threshold"]},
            "possible_reason_buckets": ["weak_colmap_support"],
        }],
    })
    repair = build_colmap_support_repair(
        weak_report,
        package,
        tmp_path / "repair",
        neighbor_radius=1,
        max_anchors_per_target=1,
        min_colmap_observations=1,
        min_colmap_observation_ratio=0.0,
    )
    manifest = Path(repair["outputs"]["repair_pairs"]).parent / "capture_splat_colmap_support_repair_manifest.json"

    summary = run_colmap_focused_repair(
        package,
        tmp_path / "focused",
        repair_manifest=manifest,
        dry_run=True,
        colmap_binary="colmap",
        include_all_registered_images=True,
        bridge_ranges="000074-000080",
        bridge_window=3,
        preserve_existing_points=True,
    )
    loaded = load_json_strict(tmp_path / "focused" / "capture_splat_colmap_focused_repair_summary.json")

    assert summary["dry_run"] is True
    assert loaded["authority"]["quality_claim"] is False
    assert loaded["commands"][0][1] == "feature_extractor"
    assert loaded["commands"][1][1] == "matches_importer"
    assert loaded["commands"][2][1] == "point_triangulator"
    assert "--SiftExtraction.use_gpu" in loaded["commands"][0]
    assert "--SiftMatching.use_gpu" in loaded["commands"][1]
    assert "--SiftMatching.guided_matching" in loaded["commands"][1]
    assert loaded["commands"][2][loaded["commands"][2].index("--clear_points") + 1] == "0"
    assert loaded["repair_inputs"]["selected_image_count"] == 4
    assert loaded["repair_inputs"]["added_bridge_pair_count"] > 0
    assert loaded["preserve_existing_points"] is True
