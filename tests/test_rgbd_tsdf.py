from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from capture_splat.json_utils import load_json_strict, write_json_strict
from capture_splat.rgbd_tsdf import (
    ARKIT_TO_OPENCV_CAMERA,
    MESH_NAME,
    REPORT_NAME,
    arkit_camera_to_open3d_extrinsic,
    build_rgbd_tsdf,
)
from capture_splat.world_studio_export import MANIFEST_NAME


def sha256(path: Path) -> str:
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def file_ref(path: Path, root: Path) -> dict[str, object]:
    return {
        "path": path.relative_to(root).as_posix(),
        "size_bytes": path.stat().st_size,
        "checksum": sha256(path),
    }


def name_digest(names: list[str]) -> str:
    digest = hashlib.sha256()
    for name in sorted(names):
        digest.update(name.encode())
        digest.update(b"\n")
    return f"sha256:{digest.hexdigest()}"


def write_handoff(root: Path) -> Path:
    frames = []
    for index, translation in enumerate((0.0, 0.04), start=1):
        stem = f"{index:06d}"
        rgb = root / "images" / f"{stem}.png"
        depth = root / "depth" / f"{stem}.npy"
        confidence = root / "confidence" / f"{stem}.npy"
        mask = root / "masks/valid" / f"{stem}.png"
        person = root / "masks/person" / f"{stem}.png" if index == 1 else None
        for path in (rgb, depth, confidence, mask, person):
            if path is None:
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
        color = np.zeros((48, 64, 3), dtype=np.uint8)
        color[..., 0] = np.arange(64, dtype=np.uint8)
        color[..., 1] = 96 + index
        Image.fromarray(color).save(rgb)
        np.save(depth, np.ones((48, 64), dtype=np.float32), allow_pickle=False)
        np.save(confidence, np.full((48, 64), 2, dtype=np.uint8), allow_pickle=False)
        Image.fromarray(np.full((48, 64), 255, dtype=np.uint8)).save(mask)
        if person is not None:
            person_pixels = np.zeros((48, 64), dtype=np.uint8)
            person_pixels[20:28, 28:36] = 255
            Image.fromarray(person_pixels).save(person)
        transform = np.eye(4)
        transform[0, 3] = translation
        frames.append({
            "accepted": True,
            "rgb": rgb.relative_to(root).as_posix(),
            "depth": depth.relative_to(root).as_posix(),
            "confidence": confidence.relative_to(root).as_posix(),
            "valid_mask": mask.relative_to(root).as_posix(),
            "intrinsics": {
                "fl_x": 50.0,
                "fl_y": 50.0,
                "cx": 31.5,
                "cy": 23.5,
                "w": 64,
                "h": 48,
            },
            "transform_matrix": transform.tolist(),
        })
        if person is not None:
            frames[-1]["person_mask"] = person.relative_to(root).as_posix()
    capture_path = root / "capture.json"
    write_json_strict(capture_path, {
        "schema": "capture_splat.v0.3",
        "session_config": {
            "scale_authority": "arkit_vio_metric",
            "up_axis": [0, 1, 0],
            "world_alignment": "gravity",
        },
        "frames": frames,
    })
    sparse = root / "sparse/0"
    sparse.mkdir(parents=True)
    images_path = sparse / "images.txt"
    registered = [f"images/{index:06d}.png" for index in (1, 2)]
    images_path.write_text(
        "\n".join(
            line
            for index, name in enumerate(registered, start=1)
            for line in (f"{index} 1 0 0 0 0 0 0 {index} {name}", "")
        ) + "\n",
        encoding="utf-8",
    )
    references = sorted(
        value
        for frame in frames
        for key in ("rgb", "depth", "confidence", "valid_mask", "person_mask")
        if isinstance((value := frame.get(key)), str)
    )
    inventory = [file_ref(root / relative, root) for relative in references]
    matched = [Path(name).name for name in registered]
    handoff_path = root / MANIFEST_NAME
    write_json_strict(handoff_path, {
        "schema": "capture_splat.world_studio_handoff.v0.3",
        "assets": {
            "capture_manifest": file_ref(capture_path, root),
            "colmap_sparse": {"images.txt": file_ref(images_path, root)},
        },
        "capture_manifest_assets": {
            "schema": "capture_splat.capture_manifest_assets.v0.1",
            "complete": True,
            "decision": "ready",
            "missing": [],
            "conflicts": [],
            "unique_asset_count": len(inventory),
            "verified_asset_count": len(inventory),
            "assets": inventory,
        },
        "training_dataset": {
            "schema": "capture_splat.training_dataset.v0.1",
            "evidence": {
                "sfm": {
                    "registered_image_parse_status": "complete",
                    "registered_image_invalid_record_count": 0,
                    "registered_image_count": len(registered),
                    "registered_image_name_digest": name_digest(registered),
                    "registered_rgbd_overlap": {
                        "available": True,
                        "depth_bearing_capture_frame_count": len(frames),
                        "matched_count": len(matched),
                        "matched_name_digest": name_digest(matched),
                        "ambiguous_basename_count": 0,
                        "unmatched_registered_image_count": 0,
                    },
                }
            },
        },
    })
    return handoff_path


def test_arkit_camera_conversion_maps_negative_z_forward_to_open3d_positive_z() -> None:
    camera_to_world = np.eye(4)
    camera_to_world[:3, 3] = [1.0, 2.0, 3.0]

    extrinsic = arkit_camera_to_open3d_extrinsic(camera_to_world)
    world_point = camera_to_world @ np.asarray([0.0, 0.0, -2.0, 1.0])

    assert np.allclose(extrinsic, np.linalg.inv(camera_to_world @ ARKIT_TO_OPENCV_CAMERA))
    assert np.allclose(extrinsic @ world_point, [0.0, 0.0, 2.0, 1.0])


def test_three_plane_coordinate_fixture_preserves_front_floor_and_wall_axes() -> None:
    opencv_plane = np.asarray([
        [-0.4, -0.3, 1.0, 1.0],
        [0.4, -0.3, 1.0, 1.0],
        [-0.4, 0.3, 1.0, 1.0],
    ])
    rotations = {
        "front": np.eye(3),
        "floor": np.column_stack(([1, 0, 0], [0, 0, -1], [0, 1, 0])),
        "wall": np.column_stack(([0, 0, 1], [0, 1, 0], [-1, 0, 0])),
    }
    world_planes: dict[str, np.ndarray] = {}
    for name, rotation in rotations.items():
        camera_to_world = np.eye(4)
        camera_to_world[:3, :3] = rotation
        world_planes[name] = (
            np.linalg.inv(arkit_camera_to_open3d_extrinsic(camera_to_world))
            @ opencv_plane.T
        ).T[:, :3]

    assert np.allclose(world_planes["front"][:, 2], -1.0)
    assert np.ptp(world_planes["front"][:, 0]) > 0
    assert np.ptp(world_planes["front"][:, 1]) > 0
    assert np.allclose(world_planes["floor"][:, 1], -1.0)
    assert np.ptp(world_planes["floor"][:, 0]) > 0
    assert np.ptp(world_planes["floor"][:, 2]) > 0
    assert np.allclose(world_planes["wall"][:, 0], 1.0)
    assert np.ptp(world_planes["wall"][:, 1]) > 0
    assert np.ptp(world_planes["wall"][:, 2]) > 0


def test_rgbd_tsdf_rejects_output_inside_handoff(tmp_path: Path) -> None:
    handoff = write_handoff(tmp_path / "handoff")

    with pytest.raises(ValueError, match="outside the immutable handoff"):
        build_rgbd_tsdf(handoff, handoff.parent / "derived")

    assert not (handoff.parent / "derived").exists()


def test_rgbd_tsdf_rejects_missing_metric_coordinate_declaration(tmp_path: Path) -> None:
    handoff = write_handoff(tmp_path / "handoff")
    capture_path = handoff.parent / "capture.json"
    capture = load_json_strict(capture_path)
    capture["session_config"].pop("world_alignment")
    write_json_strict(capture_path, capture)
    manifest = load_json_strict(handoff)
    manifest["assets"]["capture_manifest"] = file_ref(capture_path, handoff.parent)
    write_json_strict(handoff, manifest)

    with pytest.raises(ValueError, match="ARKit metric gravity-aligned"):
        build_rgbd_tsdf(handoff, tmp_path / "output")


def test_rgbd_tsdf_rejects_a_tampered_consumed_asset(tmp_path: Path) -> None:
    handoff = write_handoff(tmp_path / "handoff")
    np.save(handoff.parent / "depth/000001.npy", np.zeros((48, 64), dtype=np.float32))

    with pytest.raises(ValueError, match="checksum mismatch"):
        build_rgbd_tsdf(handoff, tmp_path / "output")

    report = load_json_strict(tmp_path / "output" / REPORT_NAME)
    assert report["decision"] == "reject"
    assert not any(report["authority"].values())


def test_rgbd_tsdf_rejects_non_authoritative_depth_dtype_before_fusion(tmp_path: Path) -> None:
    handoff = write_handoff(tmp_path / "handoff")
    depth_path = handoff.parent / "depth/000001.npy"
    np.save(depth_path, np.ones((48, 64), dtype=np.float64), allow_pickle=False)
    manifest = load_json_strict(handoff)
    replacement = file_ref(depth_path, handoff.parent)
    manifest["capture_manifest_assets"]["assets"] = [
        replacement if asset["path"] == replacement["path"] else asset
        for asset in manifest["capture_manifest_assets"]["assets"]
    ]
    write_json_strict(handoff, manifest)

    with pytest.raises(ValueError, match="depth must be a 2D float32 NPY"):
        build_rgbd_tsdf(handoff, tmp_path / "output")


def test_rgbd_tsdf_rejects_non_rigid_pose_before_fusion(tmp_path: Path) -> None:
    handoff = write_handoff(tmp_path / "handoff")
    capture_path = handoff.parent / "capture.json"
    capture = load_json_strict(capture_path)
    capture["frames"][0]["transform_matrix"][0][0] = 2.0
    write_json_strict(capture_path, capture)
    manifest = load_json_strict(handoff)
    manifest["assets"]["capture_manifest"] = file_ref(capture_path, handoff.parent)
    write_json_strict(handoff, manifest)

    with pytest.raises(ValueError, match="rotation is not rigid"):
        build_rgbd_tsdf(handoff, tmp_path / "output")


def test_rgbd_tsdf_synthetic_mesh_is_finite_held_and_deterministic(tmp_path: Path) -> None:
    pytest.importorskip("open3d", reason="install capture-splat[tsdf] for the TSDF smoke test")
    handoff = write_handoff(tmp_path / "handoff")

    first = build_rgbd_tsdf(handoff.parent, tmp_path / "first")
    second = build_rgbd_tsdf(handoff, tmp_path / "second")

    assert first["decision"] == "hold"
    assert first["software_surface_candidate"] == "hold"
    assert first["mesh"]["software_surface_candidate"] == "hold"
    assert first["coverage"]["integrated_frame_count"] == 2
    assert first["coverage"]["person_mask_frame_count"] == 1
    assert first["coverage"]["filter_counts"]["person_mask_pixel_count"] > 0
    assert first["coverage"]["filter_counts"]["person_mask_excluded_pixel_count"] > 0
    assert first["coverage"]["dynamic_cleanup_complete"] is False
    assert first["mesh"]["finite"] is True
    assert first["mesh"]["vertex_count"] > 0
    assert first["mesh"]["triangle_count"] > 0
    assert first["mesh"]["checksum"] == second["mesh"]["checksum"]
    assert first["inputs"]["ordered_registered_rgbd_frame_digest"] == second["inputs"]["ordered_registered_rgbd_frame_digest"]
    assert first["coordinate_contract"]["capture_declaration"]["scale_authority"] == "arkit_vio_metric"
    assert first["performance"]["measurement"]["integrated_frames_per_second"] > 0
    assert first["performance"]["measurement"]["valid_megapixels_per_second"] > 0
    assert not any(first["authority"].values())
    assert first["authority"]["metric_authority"] is False
    assert (tmp_path / "first" / MESH_NAME).is_file()
