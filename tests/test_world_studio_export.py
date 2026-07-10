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
    write_json_strict(capture / "capture.json", {
        "schema": "capture_splat.v0.3",
        "intrinsics": {"fl_x": 8, "fl_y": 6, "cx": 4, "cy": 3, "w": 8, "h": 6},
        "arkit_mesh_file": "geometry/arkit_mesh.ply",
        "arkit_mesh_report_file": "geometry/arkit_mesh_report.json",
        "room_plan_semantics_file": "room_plan/room_semantics.json",
        "frame_index_file": "metadata/frame_index.jsonl",
        "frames": capture_frames,
    })
    write_ascii_ply(capture / "geometry" / "arkit_mesh.ply")
    write_json_strict(capture / "geometry" / "arkit_mesh_report.json", {"status": "finite_mesh_written"})
    write_json_strict(capture / "room_plan" / "room_semantics.json", {"schema": "capture_splat.room_semantics.v0.1"})
    frame_index = capture / "metadata" / "frame_index.jsonl"
    frame_index.parent.mkdir(parents=True, exist_ok=True)
    frame_index.write_text('{"video_frame_index":0}\n', encoding="utf-8")

    export_world_studio_handoff(
        package,
        tmp_path / "world_studio",
        gaussian=run / "splat.ply",
        capture_manifest=capture / "capture.json",
        copy_files=True,
    )
    manifest = load_json_strict(tmp_path / "world_studio" / MANIFEST_NAME)

    assert manifest["assets"]["navigation_mesh"]["path"] == "navigation_mesh.ply"
    assert manifest["assets"]["navigation_mesh"]["coordinate_frame"] == "arkit_world"
    assert manifest["assets"]["navigation_mesh"]["units"] == "meters"
    assert manifest["assets"]["mesh_report"]["path"] == "navigation_mesh_report.json"
    assert manifest["assets"]["room_semantics"]["coordinate_frame"] == "roomplan_world_unregistered"
    assert manifest["assets"]["camera_trajectory"]["path"] == "camera_trajectory.jsonl"
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
