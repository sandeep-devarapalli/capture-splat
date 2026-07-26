from pathlib import Path

import numpy as np
from PIL import Image

from capture_splat.json_utils import load_json_strict, write_json_strict
from capture_splat.world_studio_export import (
    MANIFEST_NAME,
    _mesh_walk_evidence,
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

    assert summary["schema"] == "capture_splat.world_studio_handoff.v0.2"
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
    assert manifest["assets"]["gaussian_ply"]["variant"] == "raw"


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


def test_export_world_studio_v02_scene_fields(tmp_path: Path) -> None:
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

    assert manifest["schema"] == "capture_splat.world_studio_handoff.v0.2"
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

    export_world_studio_handoff(
        package,
        tmp_path / "world_studio",
        gaussian=run / "splat.ply",
        capture_manifest=capture / "capture.json",
        measurement_points=metric_seed,
        measurement_points_frame="metric_colmap_world",
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
    assert manifest["measurement_eligibility"]["status"] == "held"
    assert manifest["measurement_eligibility"]["software_prerequisites"] is True
    assert manifest["measurement_eligibility"]["reason"] == "physical_known_distance_validation_pending"
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
