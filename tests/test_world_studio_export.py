from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from capture_splat.json_utils import load_json_strict, write_json_strict
from capture_splat.world_studio_export import (
    MANIFEST_NAME,
    _measurement_eligibility,
    _mesh_walk_evidence,
    _registered_image_names,
    _registered_rgbd_overlap,
    _sha256,
    export_world_studio_handoff,
)


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


def test_registered_image_evidence_requires_complete_record_pairs(tmp_path: Path) -> None:
    pose = "1 1 0 0 0 0 0 0 1 000001.jpg"
    truncated = tmp_path / "truncated.txt"
    truncated.write_text(f"{pose}\n", encoding="utf-8")
    assert _registered_image_names(truncated) == (["000001.jpg"], 1)

    malformed = tmp_path / "malformed.txt"
    malformed.write_text(f"{pose}\nnan 0 -1\n", encoding="utf-8")
    assert _registered_image_names(malformed) == (["000001.jpg"], 1)

    complete = tmp_path / "complete.txt"
    complete.write_text(f"{pose}\n\n", encoding="utf-8")
    assert _registered_image_names(complete) == (["000001.jpg"], 0)

    malformed_poses = tmp_path / "malformed_poses.txt"
    malformed_poses.write_text(
        "2 nan 0 0 0 0 0 0 1 nonfinite.jpg\n\n"
        "3 0 0 0 0 0 0 0 1 zero_quaternion.jpg\n\n",
        encoding="utf-8",
    )
    assert _registered_image_names(malformed_poses) == ([], 2)


def test_registered_rgbd_overlap_requires_one_authoritative_root(tmp_path: Path) -> None:
    package = tmp_path / "package"
    capture_root = tmp_path / "capture"
    write_image(package / "rgb/000001.jpg")
    (capture_root / "depth").mkdir(parents=True)
    np.save(capture_root / "depth/000001.npy", np.ones((2, 2), dtype=np.float32))
    capture = {"frames": [{"rgb": "rgb/000001.jpg", "depth": "depth/000001.npy"}]}

    separated = _registered_rgbd_overlap(
        ["000001.jpg"], capture, capture_root, package, True,
    )
    assert separated["matched_count"] == 0
    assert separated["depth_bearing_capture_frame_count"] == 0

    write_image(capture_root / "rgb/000001.jpg")
    paired = _registered_rgbd_overlap(["000001.jpg"], capture, capture_root, package, True)
    assert paired["matched_count"] == paired["depth_bearing_capture_frame_count"] == 1


def test_mesh_walk_evidence_holds_legacy_truncated_mesh(tmp_path: Path) -> None:
    report = tmp_path / "arkit_mesh_report.json"
    write_json_strict(report, {
        "status": "finite_mesh_written",
        "non_finite_vertex_count": 0,
        "truncated": True,
    })

    assert _mesh_walk_evidence(report) == {
        "status": "held",
        "reason": "source_mesh_truncated",
    }


def test_mesh_walk_evidence_accepts_explicit_spatial_coverage(tmp_path: Path) -> None:
    report = tmp_path / "arkit_mesh_report.json"
    write_json_strict(report, {
        "schema": "capture_splat.arkit_mesh_report.v0.2",
        "status": "finite_mesh_written",
        "non_finite_vertex_count": 0,
        "budget_limited": True,
        "coverage_preserving": True,
        "eligible_anchor_count": 12,
        "exported_anchor_count": 12,
        "anchor_coverage_ratio": 1.0,
        "source_spatial_cell_count": 9,
        "exported_spatial_cell_count": 9,
        "spatial_cell_coverage_ratio": 1.0,
        "selection_policy": "anchor_spatial_stratified_even_faces_v1",
    })

    assert _mesh_walk_evidence(report) == {
        "status": "accepted",
        "reason": "coverage_preserving_budgeted_mesh",
        "selection_policy": "anchor_spatial_stratified_even_faces_v1",
    }


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

    assert summary["schema"] == "capture_splat.world_studio_handoff.v0.3"
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


def test_export_world_studio_records_sanitized_training_dataset(tmp_path: Path) -> None:
    package = tmp_path / "colmap_package"
    write_image(package / "images" / "000001.jpg")
    write_image(package / "images" / "000002.jpg")
    sparse = package / "sparse" / "0"
    sparse.mkdir(parents=True)
    (sparse / "cameras.txt").write_text(
        "1 PINHOLE 8 6 8 8 4 3\n2 OPENCV 8 6 8 8 4 3 0 0 0 0\n",
        encoding="utf-8",
    )
    (sparse / "images.txt").write_text(
        "1 1 0 0 0 0 0 0 1 000001.jpg\n\n2 1 0 0 0 -1 0 0 2 000002.jpg\n\n",
        encoding="utf-8",
    )
    (sparse / "points3D.txt").write_text("# empty\n", encoding="utf-8")
    write_json_strict(package / "metadata" / "source_equirectangular_rig.json", {
        "schema": "capture_splat.equirectangular_rig.v0.1",
        "projection_model": "PINHOLE",
    })

    capture = tmp_path / "capture"
    (capture / "depth").mkdir(parents=True)
    (capture / "confidence").mkdir(parents=True)
    write_image(capture / "rgb/000001.jpg")
    write_image(capture / "rgb/000002.jpg")
    np.save(capture / "depth" / "000001.npy", np.ones((2, 2), dtype=np.float32))
    np.save(capture / "confidence" / "000001.npy", np.ones((2, 2), dtype=np.uint8))
    write_image(capture / "masks" / "person" / "000001.png")
    write_image(capture / "masks" / "valid" / "000002.png")
    write_ascii_ply(capture / "geometry" / "arkit_mesh.ply")
    write_json_strict(capture / "geometry" / "arkit_mesh_report.json", {
        "status": "finite_mesh_written",
        "non_finite_vertex_count": 0,
    })
    write_json_strict(capture / "capture.json", {
        "schema": "capture_splat.v0.3",
        "capture_profile": "video_360",
        "arkit_mesh_file": "geometry/arkit_mesh.ply",
        "arkit_mesh_report_file": "geometry/arkit_mesh_report.json",
        "frames": [
            {
                "rgb": "rgb/000001.jpg",
                "depth": "depth/000001.npy",
                "confidence": "confidence/000001.npy",
                "person_mask": "masks/person/000001.png",
                "capture_quality": {"accepted": True},
                "transform_matrix": [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]],
            },
            {
                "rgb": "rgb/000002.jpg",
                "depth": "depth/000002.npy",
                "valid_mask": "masks/valid/000002.png",
                "capture_quality": {"accepted": True},
                "transform_matrix": [[1, 0, 0, 1], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]],
            },
        ],
    })

    export_world_studio_handoff(
        package,
        tmp_path / "world_studio_a",
        capture_manifest=capture / "capture.json",
        copy_files=False,
    )
    export_world_studio_handoff(
        package,
        tmp_path / "world_studio_b",
        capture_manifest=capture / "capture.json",
        copy_files=False,
    )
    manifest = load_json_strict(tmp_path / "world_studio_a" / MANIFEST_NAME)
    second = load_json_strict(tmp_path / "world_studio_b" / MANIFEST_NAME)
    dataset = manifest["training_dataset"]

    assert dataset["schema"] == "capture_splat.training_dataset.v0.1"
    assert dataset["capture_profile"] == "video_360"
    assert dataset["source_frame_set"]["count"] == 2
    assert dataset["source_frame_set"]["digest"].startswith("sha256:")
    assert dataset["source_frame_set"] == second["training_dataset"]["source_frame_set"]
    assert dataset["projection"]["mode"] == "projected_pinhole_from_equirectangular"
    assert dataset["projection"]["training_images_are_projected_pinhole"] is True
    assert dataset["projection"]["native_equirectangular"] is False
    assert dataset["evidence"]["sfm"]["camera_count"] == 2
    assert dataset["evidence"]["sfm"]["camera_models"] == ["OPENCV", "PINHOLE"]
    assert dataset["evidence"]["sfm"]["registered_image_count"] == 2
    assert dataset["evidence"]["sfm"]["registered_image_parse_status"] == "complete"
    assert dataset["evidence"]["sfm"]["registered_rgbd_overlap_count"] == 1
    assert dataset["evidence"]["sfm"]["registered_rgbd_overlap"] == {
        "available": True,
        "matching": "unique_case_sensitive_rgb_basename_with_same_root_rgb_and_depth_v1",
        "depth_bearing_capture_frame_count": 1,
        "matched_count": 1,
        "matched_name_digest": dataset["evidence"]["sfm"]["registered_rgbd_overlap"]["matched_name_digest"],
        "ambiguous_basename_count": 0,
        "unmatched_registered_image_count": 1,
    }
    assert dataset["evidence"]["sfm"]["registered_rgbd_overlap"]["matched_name_digest"].startswith("sha256:")
    assert dataset["evidence"]["depth"] == {
        "referenced_frame_count": 2,
        "available_frame_count": 1,
    }
    assert dataset["evidence"]["confidence"] == {
        "referenced_frame_count": 1,
        "available_frame_count": 1,
    }
    assert dataset["evidence"]["masks"] == {
        "referenced_frame_count": 2,
        "available_frame_count": 2,
    }
    assert dataset["evidence"]["mesh"]["available"] is True
    assert dataset["authority"]["capture_evidence_only"] is True
    assert dataset["authority"]["trainer_consumption_claim"] is False
    assert str(tmp_path) not in str(dataset)


def test_export_world_studio_distinguishes_native_equirectangular_evidence(tmp_path: Path) -> None:
    package = tmp_path / "native_360"
    write_image(package / "images" / "000001.jpg")
    write_json_strict(package / "metadata" / "equirectangular_rig.json", {
        "schema": "capture_splat.equirectangular_rig.v0.1",
        "projection_model": "EQUIRECTANGULAR",
    })

    export_world_studio_handoff(
        package,
        tmp_path / "world_studio",
        capture_profile="video_360",
        copy_files=True,
    )
    dataset = load_json_strict(tmp_path / "world_studio" / MANIFEST_NAME)["training_dataset"]

    assert dataset["projection"]["mode"] == "native_equirectangular"
    assert dataset["projection"]["native_equirectangular"] is True
    assert dataset["projection"]["training_images_are_projected_pinhole"] is False


def test_export_world_studio_attaches_quality_evidence(tmp_path: Path) -> None:
    package = tmp_path / "colmap_package"
    write_image(package / "images" / "000001.jpg")
    write_ascii_ply(tmp_path / "splat.ply")
    qa = tmp_path / "capture_splat_render_source_qa_summary.json"
    write_json_strict(
        qa,
        {
            "schema": "capture_splat.render_source_qa.v0.1",
            "decision": "hold",
            "frame_count": 38,
            "valid_frame_count": 38,
            "weak_frames": ["000233", "000249"],
        },
    )
    export_world_studio_handoff(
        package,
        tmp_path / "world_studio",
        gaussian=tmp_path / "splat.ply",
        render_source_qa=qa,
        copy_files=True,
    )
    manifest = load_json_strict(tmp_path / "world_studio" / MANIFEST_NAME)
    stats = load_json_strict(tmp_path / "world_studio" / "quality" / "ply_stats.json")

    assert manifest["assets"]["render_source_qa"]["path"] == "quality/render_source_qa.json"
    assert manifest["assets"]["ply_stats"]["path"] == "quality/ply_stats.json"
    assert manifest["assets"]["render_source_qa"]["checksum"].startswith("sha256:")
    assert manifest["assets"]["ply_stats"]["checksum"].startswith("sha256:")
    assert stats["path"] == "splat.ply"
    assert stats["finite"] is True
    assert stats["splat_count"] == 1


def test_export_world_studio_rejects_invalid_quality_schema(tmp_path: Path) -> None:
    package = tmp_path / "colmap_package"
    write_image(package / "images" / "000001.jpg")
    write_ascii_ply(tmp_path / "splat.ply")
    qa = tmp_path / "capture_splat_render_source_qa_summary.json"
    write_json_strict(
        qa,
        {
            "schema": "unknown.qa.v1",
            "decision": "hold",
            "frame_count": 1,
            "valid_frame_count": 1,
            "weak_frames": [],
        },
    )

    try:
        export_world_studio_handoff(
            package,
            tmp_path / "world_studio",
            gaussian=tmp_path / "splat.ply",
            render_source_qa=qa,
        )
    except ValueError as error:
        assert "unsupported schema" in str(error)
    else:
        raise AssertionError("expected invalid render/source QA schema to fail")


def test_export_world_studio_records_non_finite_selected_gaussian(tmp_path: Path) -> None:
    package = tmp_path / "colmap_package"
    write_image(package / "images" / "000001.jpg")
    gaussian = tmp_path / "splat.ply"
    write_ascii_ply(gaussian)
    gaussian.write_text(gaussian.read_text(encoding="ascii").replace("0 0 0\n", "nan 0 0\n"), encoding="ascii")

    export_world_studio_handoff(
        package,
        tmp_path / "world_studio",
        gaussian=gaussian,
        copy_files=True,
    )
    stats = load_json_strict(tmp_path / "world_studio" / "quality" / "ply_stats.json")

    assert stats["path"] == "splat.ply"
    assert stats["finite"] is False
    assert stats["non_finite_count"] == 1


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
    assert not (tmp_path / "world_studio").exists()
    assert not list(tmp_path.glob(".world_studio.*.partial"))


def test_export_world_studio_rejects_existing_package_target(tmp_path: Path) -> None:
    package = tmp_path / "colmap_package"
    write_image(package / "images" / "000001.jpg")
    write_ascii_ply(package / "splat.ply")
    image_sha = _sha256(package / "images/000001.jpg")
    splat_sha = _sha256(package / "splat.ply")

    with pytest.raises(FileExistsError, match="refusing to replace"):
        export_world_studio_handoff(package, package, copy_files=False)

    assert _sha256(package / "images/000001.jpg") == image_sha
    assert _sha256(package / "splat.ply") == splat_sha
    assert not (package / MANIFEST_NAME).exists()


def test_export_world_studio_never_replaces_existing_target(tmp_path: Path) -> None:
    package = tmp_path / "package"
    write_image(package / "images/000001.jpg")
    target = tmp_path / "handoff"
    target.mkdir()
    sentinel = target / "sentinel"
    sentinel.write_bytes(b"preserve")

    with pytest.raises(FileExistsError, match="refusing to replace"):
        export_world_studio_handoff(package, target, copy_files=True)
    assert sentinel.read_bytes() == b"preserve"
    assert list(target.iterdir()) == [sentinel]
    assert not list(tmp_path.glob(".handoff.*.partial"))

    file_target = tmp_path / "handoff-file"
    file_target.write_bytes(b"file")
    with pytest.raises(FileExistsError, match="refusing to replace"):
        export_world_studio_handoff(package, file_target, copy_files=True)
    assert file_target.read_bytes() == b"file"
    assert not list(tmp_path.glob(".handoff-file.*.partial"))

    outside = tmp_path / "outside"
    outside.mkdir()
    outside_sentinel = outside / "sentinel"
    outside_sentinel.write_bytes(b"outside")
    alias = tmp_path / "handoff-link"
    alias.symlink_to(outside, target_is_directory=True)
    with pytest.raises(FileExistsError, match="refusing to replace"):
        export_world_studio_handoff(package, alias, copy_files=True)
    assert alias.is_symlink()
    assert outside_sentinel.read_bytes() == b"outside"
    assert not list(tmp_path.glob(".handoff-link.*.partial"))


def test_export_world_studio_publication_race_leaves_winner_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = tmp_path / "package"
    write_image(package / "images/000001.jpg")
    target = tmp_path / "handoff"

    def lose_publication_race(source: Path, destination: Path) -> None:
        destination.mkdir()
        (destination / "sentinel").write_bytes(b"winner")
        raise FileExistsError(destination)

    monkeypatch.setattr(
        "capture_splat.world_studio_export._publish_exclusive",
        lose_publication_race,
    )
    with pytest.raises(FileExistsError):
        export_world_studio_handoff(package, target, copy_files=True)
    assert (target / "sentinel").read_bytes() == b"winner"
    assert list(target.iterdir()) == [target / "sentinel"]
    assert not list(tmp_path.glob(".handoff.*.partial"))


def test_export_world_studio_prefers_alpha_pruned_gaussian(tmp_path: Path) -> None:
    package = tmp_path / "colmap_package"
    write_image(package / "images" / "000001.jpg")
    write_ascii_ply(package / "splat.ply")
    write_ascii_ply(package / "splat.pruned_a12.ply")

    export_world_studio_handoff(package, tmp_path / "world_studio", copy_files=True)
    manifest = load_json_strict(tmp_path / "world_studio" / MANIFEST_NAME)

    gaussian = manifest["assets"]["gaussian_ply"]
    assert gaussian["path"] == "splat.ply"
    assert gaussian["variant"] == "alpha_pruned"
    assert gaussian["source_name"] == "splat.pruned_a12.ply"


def test_export_world_studio_v03_scene_fields(tmp_path: Path) -> None:
    package = tmp_path / "colmap_package"
    write_image(package / "images" / "000001.jpg")
    sparse = package / "sparse" / "0"
    sparse.mkdir(parents=True)
    (sparse / "cameras.txt").write_text("1 PINHOLE 8 6 8 6 4 3\n", encoding="utf-8")
    (sparse / "images.txt").write_text("1 1 0 0 0 0.5 0.1 2.0 1 000001.jpg\n\n", encoding="utf-8")
    (sparse / "points3D.txt").write_text("# empty\n", encoding="utf-8")
    rows = [f"{x} {y} 0 0" for x in (-1.0, -0.5, 0.0, 0.5, 1.0) for y in (-1.0, 0.0, 1.0)]
    (package / "splat.ply").write_text(
        "\n".join([
            "ply",
            "format ascii 1.0",
            f"element vertex {len(rows)}",
            "property float x",
            "property float y",
            "property float z",
            "property float opacity",
            "end_header",
            *rows,
        ]) + "\n",
        encoding="ascii",
    )
    write_json_strict(package / "capture_splat_scene_transform.json", {
        "schema": "capture_splat.scene_transform.v0.1",
        "trainer": "vksplat",
        "ply": "splat.ply",
        "trainer_transform": [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]],
        "trainer_transform_source": "trainer_train_json",
    })
    write_json_strict(package / "capture.json", {"schema": "capture_splat.v0.2", "capture_profile": "room_interior", "frames": [{"rgb": "images/000001.jpg", "transform_matrix": [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]], "intrinsics": {"fl_x": 8, "fl_y": 6, "cx": 4, "cy": 3, "w": 8, "h": 6}}]})

    export_world_studio_handoff(package, tmp_path / "world_studio", copy_files=True)
    manifest = load_json_strict(tmp_path / "world_studio" / MANIFEST_NAME)

    assert manifest["schema"] == "capture_splat.world_studio_handoff.v0.3"
    assert manifest["scene_transform"]["trainer_transform_source"] == "trainer_train_json"
    assert manifest["dataparser_transform"] == manifest["scene_transform"]["trainer_transform"]
    assert manifest["scene_radius"] > 0
    assert manifest["median_structure_distance"] <= manifest["scene_radius"]
    assert manifest["capture_profile"] == "room_interior"
    camera = manifest["initial_camera"]
    assert camera["mode"] == "inside"
    assert camera["coordinate_frame"] == "colmap_world"
    assert camera["position"] == [-0.5, -0.1, -2.0]


def test_export_world_studio_explicit_gaussian_wins_over_pruned(tmp_path: Path) -> None:
    package = tmp_path / "colmap_package"
    write_image(package / "images" / "000001.jpg")
    write_ascii_ply(package / "splat.ply")
    write_ascii_ply(package / "splat.pruned_a12.ply")

    export_world_studio_handoff(package, tmp_path / "world_studio", gaussian=package / "splat.ply", copy_files=True)
    manifest = load_json_strict(tmp_path / "world_studio" / MANIFEST_NAME)

    assert manifest["assets"]["gaussian_ply"]["variant"] == "raw"


def test_export_world_studio_registers_capture_metric_sidecars(tmp_path: Path) -> None:
    package = tmp_path / "colmap_package"
    sparse = package / "sparse" / "0"
    sparse.mkdir(parents=True)
    (sparse / "cameras.txt").write_text("1 PINHOLE 8 6 8 6 4 3\n", encoding="utf-8")
    centers = [
        (0, 0, 0),
        (1, 0, 0),
        (0, 1, 0),
        (0, 0, 1),
        (1, 1, 0),
        (1, 0, 1),
        (0, 1, 1),
        (1, 1, 1),
    ]
    image_lines = []
    capture_frames = []
    for index, center in enumerate(centers, start=1):
        name = f"{index:06d}.jpg"
        write_image(package / "images" / name)
        image_lines.extend([
            f"{index} 1 0 0 0 {-center[0]} {-center[1]} {-center[2]} 1 {name}",
            "",
        ])
        capture_frames.append({
            "rgb": f"rgb/{name}",
            "depth": f"depth/{index:06d}.npy",
            "accepted": True,
            "transform_matrix": [
                [1, 0, 0, center[0]],
                [0, 1, 0, center[1]],
                [0, 0, 1, center[2]],
                [0, 0, 0, 1],
            ],
        })
    (sparse / "images.txt").write_text("\n".join(image_lines), encoding="utf-8")
    (sparse / "points3D.txt").write_text("# empty\n", encoding="utf-8")

    run = tmp_path / "run"
    write_ascii_ply(run / "splat.ply")
    write_json_strict(run / "train.json", {
        "dataparser_transform": [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]],
    })
    capture = tmp_path / "capture"
    (capture / "depth").mkdir(parents=True)
    for index in range(1, len(capture_frames) + 1):
        write_image(capture / f"rgb/{index:06d}.jpg")
        np.save(capture / f"depth/{index:06d}.npy", np.ones((3, 4), dtype=np.float32))
    write_json_strict(capture / "capture.json", {
        "schema": "capture_splat.v0.3",
        "intrinsics": {"fl_x": 8, "fl_y": 6, "cx": 4, "cy": 3, "w": 8, "h": 6},
        "arkit_mesh_file": "geometry/arkit_mesh.ply",
        "arkit_mesh_report_file": "geometry/arkit_mesh_report.json",
        "room_plan_semantics_file": "room_plan/room_semantics.json",
        "room_plan_file": "room_plan/room.usdz",
        "room_plan_report_file": "room_plan/room_plan_report.json",
        "frame_index_file": "metadata/frame_index.jsonl",
        "planes_file": "metadata/planes.json",
        "spatial_guidance_report_file": "metadata/spatial_guidance_report.json",
        "source_capture_manifest_file": "metadata/source_capture.json",
        "session_config": {"up_axis": [0, 1, 0], "scale_authority": "arkit_vio_metric"},
        "frames": capture_frames,
    })
    write_ascii_ply(capture / "geometry" / "arkit_mesh.ply")
    write_json_strict(capture / "geometry" / "arkit_mesh_report.json", {"status": "finite_mesh_written"})
    write_json_strict(capture / "room_plan" / "room_semantics.json", {"schema": "capture_splat.room_semantics.v0.1"})
    (capture / "room_plan" / "room.usdz").write_bytes(b"usdz")
    write_json_strict(capture / "room_plan" / "room_plan_report.json", {"status": "exported"})
    frame_index = capture / "metadata" / "frame_index.jsonl"
    frame_index.parent.mkdir(parents=True, exist_ok=True)
    frame_index.write_text('{"video_frame_index":0}\n', encoding="utf-8")
    write_json_strict(capture / "metadata" / "planes.json", {"floor_y_estimate": 0.0})
    write_json_strict(capture / "metadata" / "source_capture.json", {"schema": "capture_splat.v0.3"})
    write_json_strict(capture / "metadata" / "spatial_guidance_report.json", {
        "schema": "capture_splat.spatial_guidance.v0.1",
        "authority": {"measurement": False, "collision": False, "navigation": False},
    })
    metric_seed = tmp_path / "metric_seed.ply"
    write_ascii_ply(metric_seed)
    collision_candidate = tmp_path / "collision_candidate.ply"
    write_ascii_ply(collision_candidate)
    collision_report = tmp_path / "collision_candidate_report.json"
    write_json_strict(collision_report, {
        "schema": "capture_splat.collision_candidate.v0.1",
        "decision": "hold",
        "reason": "physical_floor_wall_and_splat_registration_validation_pending",
        "software_prerequisites": True,
        "coordinate_frame": "arkit_world",
        "units": "meters",
        "candidate": {"checksum": _sha256(collision_candidate)},
    })
    write_json_strict(package / "metadata" / "metric_scale_report.json", {
        "schema": "capture_splat.metric_scale_report.v0.1",
        "status": "accepted",
        "target_units": "meters",
        "target_coordinate_frame": "metric_colmap_world",
        "meters_per_colmap_unit": 1.0,
        "authority": {"metric_scale_evidence": True},
        "output_checksums": {
            "cameras_txt": _sha256(sparse / "cameras.txt"),
            "images_txt": _sha256(sparse / "images.txt"),
            "points3D_txt": _sha256(sparse / "points3D.txt"),
            "metric_seed_ply": _sha256(metric_seed),
        },
    })
    known_scale_report = tmp_path / "apriltag_scale_report.json"
    write_json_strict(known_scale_report, {
        "schema": "capture_splat.apriltag_scale_validation.v0.1",
        "decision": "promote",
        "authority": {"known_scale_validation": True},
        "validated_artifact": {
            "checksum": _sha256(metric_seed),
            "coordinate_frame": "metric_colmap_world",
            "units": "meters",
        },
        "sparse_checksums": {
            "cameras_txt": _sha256(sparse / "cameras.txt"),
            "images_txt": _sha256(sparse / "images.txt"),
        },
    })

    export_world_studio_handoff(
        package,
        tmp_path / "world_studio",
        gaussian=run / "splat.ply",
        capture_manifest=capture / "capture.json",
        measurement_points=metric_seed,
        measurement_points_frame="metric_colmap_world",
        known_scale_report=known_scale_report,
        collision_candidate=collision_candidate,
        collision_report=collision_report,
        copy_files=True,
    )
    manifest = load_json_strict(tmp_path / "world_studio" / MANIFEST_NAME)

    assert manifest["assets"]["navigation_mesh"]["path"] == "navigation_mesh.ply"
    assert manifest["assets"]["navigation_mesh"]["coordinate_frame"] == "arkit_world"
    assert manifest["assets"]["navigation_mesh"]["units"] == "meters"
    assert manifest["assets"]["mesh_report"]["path"] == "navigation_mesh_report.json"
    assert manifest["assets"]["room_semantics"]["coordinate_frame"] == "roomplan_world_unregistered"
    assert manifest["assets"]["camera_trajectory"]["path"] == "camera_trajectory.jsonl"
    assert manifest["assets"]["spatial_guidance_report"]["path"] == "spatial_guidance_report.json"
    assert manifest["assets"]["spatial_guidance_report"]["authority"] == "capture_guidance_evidence"
    assert manifest["assets"]["planes"]["coordinate_frame"] == "arkit_world"
    assert manifest["assets"]["room_plan"]["authority"] == "semantic_geometry_proposal"
    assert manifest["assets"]["source_capture_manifest"]["path"] == "source_capture.json"
    assert manifest["assets"]["metric_scale_report"]["units"] == "meters"
    assert manifest["assets"]["measurement_points"]["coordinate_frame"] == "metric_colmap_world"
    assert manifest["assets"]["measurement_points"]["units"] == "meters"
    assert manifest["assets"]["collision_candidate"]["units"] == "meters"
    assert manifest["metric_registration"]["status"] == "accepted"
    assert manifest["metric_registration"]["matched_cameras"] == 8
    assert manifest["metric_registration"]["arkit_to_target"] == [
        [1.0, 0.0, 0.0, 0.0],
        [0.0, 1.0, 0.0, 0.0],
        [0.0, 0.0, 1.0, 0.0],
        [0.0, 0.0, 0.0, 1.0],
    ]
    assert manifest["metric_registration"]["meters_per_target_unit"] == 1.0
    assert manifest["walk_eligibility"]["status"] == "eligible"
    assert manifest["measurement_eligibility"]["status"] == "eligible"
    assert manifest["measurement_eligibility"]["software_prerequisites"] is True
    assert manifest["measurement_eligibility"]["reason"] == "known_scale_validation_accepted"
    assert manifest["measurement_eligibility"]["authority"]["measurement_authority"] is False
    held_without_physical_evidence = _measurement_eligibility(
        metric_seed,
        "metric_colmap_world",
        "meters",
        package,
        "sparse/0",
        None,
    )
    assert held_without_physical_evidence["status"] == "held"
    assert held_without_physical_evidence["reason"] == "physical_known_distance_validation_pending"
    assert manifest["collision_eligibility"]["status"] == "held"
    assert manifest["collision_eligibility"]["software_prerequisites"] is True
    assert manifest["world_up"] == [0.0, 1.0, 0.0]
    assert manifest["initial_camera"]["look_at"] == [0.0, 0.0, 1.0]
    assert manifest["initial_camera"]["up"] == [0.0, -1.0, 0.0]
    assert manifest["authority"]["navigation_authority"] is False


def test_export_world_studio_holds_walk_when_metric_registration_is_insufficient(tmp_path: Path) -> None:
    package = tmp_path / "colmap_package"
    write_image(package / "images" / "000001.jpg")
    sparse = package / "sparse" / "0"
    sparse.mkdir(parents=True)
    (sparse / "cameras.txt").write_text("1 PINHOLE 8 6 8 6 4 3\n", encoding="utf-8")
    (sparse / "images.txt").write_text("1 1 0 0 0 0 0 0 1 000001.jpg\n\n", encoding="utf-8")
    (sparse / "points3D.txt").write_text("# empty\n", encoding="utf-8")
    capture = tmp_path / "capture"
    write_json_strict(capture / "capture.json", {
        "schema": "capture_splat.v0.3",
        "intrinsics": {"fl_x": 8, "fl_y": 6, "cx": 4, "cy": 3, "w": 8, "h": 6},
        "arkit_mesh_file": "geometry/arkit_mesh.ply",
        "frames": [{
            "rgb": "rgb/000001.jpg",
            "accepted": True,
            "transform_matrix": [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]],
        }],
    })
    write_ascii_ply(capture / "geometry" / "arkit_mesh.ply")
    write_image(capture / "rgb/000001.jpg")

    export_world_studio_handoff(
        package,
        tmp_path / "world_studio",
        capture_manifest=capture / "capture.json",
        copy_files=True,
    )
    manifest = load_json_strict(tmp_path / "world_studio" / MANIFEST_NAME)

    assert manifest["metric_registration"]["status"] == "held"
    assert manifest["metric_registration"]["reason"] == "insufficient_matched_cameras"
    assert manifest["walk_eligibility"] == {
        "status": "held",
        "reason": "metric_registration_not_accepted",
        "authority": "fly_only",
    }


def test_export_world_studio_copies_every_capture_manifest_asset(tmp_path: Path) -> None:
    package = tmp_path / "package"
    write_image(package / "images/000001.jpg")
    (package / "depth").mkdir()
    np.save(package / "depth/000001.npy", np.ones((2, 2), dtype=np.float32))
    write_image(package / "masks/valid.png")
    write_json_strict(package / "metadata/planes.json", {"planes": []})
    write_json_strict(package / "pointcloud_preview/preview.json", {"point_count": 0, "points": []})
    write_json_strict(package / "capture.json", {
        "schema": "capture_splat.v0.3",
        "planes_file": "metadata/planes.json",
        "pointcloud_preview_file": "pointcloud_preview/preview.json",
        "frames": [{
            "rgb": "images/000001.jpg",
            "depth": "depth/000001.npy",
            "valid_mask": "masks/valid.png",
        }],
    })

    export_world_studio_handoff(package, tmp_path / "handoff", copy_files=True)
    manifest = load_json_strict(tmp_path / "handoff" / MANIFEST_NAME)
    evidence = manifest["capture_manifest_assets"]

    assert evidence["decision"] == "ready"
    assert evidence["verified_asset_count"] == len(evidence["assets"]) == 5
    assert {asset["path"] for asset in evidence["assets"]} == {
        "images/000001.jpg",
        "depth/000001.npy",
        "masks/valid.png",
        "metadata/planes.json",
        "pointcloud_preview/preview.json",
    }
    assert all(asset["checksum"].startswith("sha256:") for asset in evidence["assets"])
    assert not any(path.is_symlink() for path in (tmp_path / "handoff").rglob("*"))


def test_export_world_studio_capture_assets_fail_closed(tmp_path: Path) -> None:
    package = tmp_path / "package"
    write_image(package / "images/000001.jpg")

    write_json_strict(package / "capture.json", {
        "schema": "capture_splat.v0.3",
        "frames": [{"rgb": "images/000001.jpg", "depth": "depth/missing.npy"}],
    })
    with pytest.raises(ValueError, match="not self-contained"):
        export_world_studio_handoff(package, tmp_path / "missing", copy_files=True)
    assert not (tmp_path / "missing").exists()

    write_json_strict(package / "capture.json", {
        "schema": "capture_splat.v0.3",
        "planes_file": "../outside.json",
        "frames": [],
    })
    with pytest.raises(ValueError, match="escapes package"):
        export_world_studio_handoff(package, tmp_path / "escape", copy_files=True)

    (package / "Asset.bin").write_bytes(b"asset")
    write_json_strict(package / "capture.json", {
        "schema": "capture_splat.v0.3",
        "asset_file": "Asset.bin",
        "frames": [{"rgb": "asset.bin"}],
    })
    with pytest.raises(ValueError, match="case-colliding"):
        export_world_studio_handoff(package, tmp_path / "collision", copy_files=True)
