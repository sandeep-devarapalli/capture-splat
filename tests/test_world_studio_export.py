from pathlib import Path

from PIL import Image

from capture_splat.json_utils import load_json_strict, write_json_strict
from capture_splat.world_studio_export import MANIFEST_NAME, export_world_studio_handoff


def write_image(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (8, 6), (64, 96, 128)).save(path)


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


def test_export_world_studio_writes_relative_handoff_manifest(tmp_path: Path) -> None:
    package = tmp_path / "colmap_package"
    write_image(package / "images" / "000001.jpg")
    (package / "sparse" / "0").mkdir(parents=True)
    (package / "sparse" / "0" / "cameras.txt").write_text("1 PINHOLE 8 6 8 6 4 3\n", encoding="utf-8")
    (package / "sparse" / "0" / "images.txt").write_text("1 1 0 0 0 0 0 0 1 000001.jpg\n\n", encoding="utf-8")
    (package / "sparse" / "0" / "points3D.txt").write_text("# empty\n", encoding="utf-8")
    write_ascii_ply(tmp_path / "splat.ply")
    write_ascii_ply(tmp_path / "points.ply")
    write_json_strict(tmp_path / "capture.json", {"schema": "capture_splat.v0.1", "frames": []})
    write_json_strict(tmp_path / "transforms.json", {"frames": [{"file_path": "images/000001.jpg"}]})

    summary = export_world_studio_handoff(
        package,
        tmp_path / "world_studio",
        gaussian=tmp_path / "splat.ply",
        points=tmp_path / "points.ply",
        capture_manifest=tmp_path / "capture.json",
        transforms=tmp_path / "transforms.json",
        copy_files=True,
    )
    manifest = load_json_strict(tmp_path / "world_studio" / MANIFEST_NAME)

    assert summary["schema"] == "capture_splat.world_studio_handoff.v0.1"
    assert manifest["status"] == "visual_evidence_with_3dgs_proposal"
    assert manifest["source_frames"][0]["rgb_path"] == "images/000001.jpg"
    assert manifest["source_frames"][0]["source_role"] == "visual_evidence"
    assert manifest["assets"]["gaussian_ply"]["path"] == "splat.ply"
    assert manifest["assets"]["points"]["path"] == "points.ply"
    assert manifest["assets"]["capture_manifest"]["path"] == "capture.json"
    assert manifest["assets"]["transforms"]["path"] == "transforms.json"
    assert manifest["assets"]["colmap_sparse"]["images.txt"]["path"] == "sparse/0/images.txt"
    assert manifest["assets"]["gaussian_ply"]["checksum"].startswith("sha256:")
    assert manifest["authority"]["trained_splats"] == "review_proposal"
    assert manifest["authority"]["metric_authority"] is False
    assert manifest["authority"]["collision_authority"] is False
    assert manifest["authority"]["semantic_authority"] is False
    assert manifest["authority"]["navigation_authority"] is False
    assert all(not Path(frame["rgb_path"]).is_absolute() for frame in manifest["source_frames"])


def test_export_world_studio_includes_trainer_dataparser_transform(tmp_path: Path) -> None:
    package = tmp_path / "colmap_package"
    write_image(package / "images" / "000001.jpg")
    run_dir = tmp_path / "run"
    write_ascii_ply(run_dir / "splat.ply")
    transform = [[2.0, 0.0, 0.0, 10.0], [0.0, 2.0, 0.0, 0.0], [0.0, 0.0, 2.0, 0.0], [0.0, 0.0, 0.0, 1.0]]
    write_json_strict(run_dir / "train.json", {"dataparser_transform": transform})

    export_world_studio_handoff(package, tmp_path / "world_studio", gaussian=run_dir / "splat.ply", copy_files=True)
    manifest = load_json_strict(tmp_path / "world_studio" / MANIFEST_NAME)

    assert manifest["dataparser_transform"] == transform
    assert any("dataparser_transform" in note for note in manifest["notes"])


def test_export_world_studio_rejects_missing_images(tmp_path: Path) -> None:
    package = tmp_path / "empty_package"
    package.mkdir()

    try:
        export_world_studio_handoff(package, tmp_path / "world_studio")
    except FileNotFoundError as error:
        assert "no source images" in str(error)
    else:
        raise AssertionError("expected missing image package to fail")


def test_export_world_studio_can_write_into_package_without_removing_assets(tmp_path: Path) -> None:
    package = tmp_path / "colmap_package"
    write_image(package / "images" / "000001.jpg")
    write_ascii_ply(package / "splat.ply")

    export_world_studio_handoff(package, package, copy_files=False)
    manifest = load_json_strict(package / MANIFEST_NAME)

    assert (package / "images" / "000001.jpg").exists()
    assert (package / "splat.ply").exists()
    assert manifest["source_frames"][0]["rgb_path"] == "images/000001.jpg"
    assert manifest["assets"]["gaussian_ply"]["path"] == "splat.ply"
