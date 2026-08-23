import hashlib
import math
from pathlib import Path

import numpy as np
from PIL import Image
import pytest

from tests.test_capture_schema import make_capture
from capture_splat.colmap_export import arkit_camera_to_colmap_pose, export_colmap_text
from capture_splat.ingest import ingest_capture
from capture_splat.json_utils import load_json_strict, write_json_strict
from capture_splat.rgbd_seed import quaternion_rotation


def test_colmap_export_writes_text_model(tmp_path: Path) -> None:
    capture = make_capture(tmp_path / "capture")
    out = tmp_path / "out"
    summary = export_colmap_text(capture, out)
    sparse = out / "sparse" / "0"
    assert summary["image_count"] == 1
    assert (sparse / "cameras.txt").exists()
    assert (sparse / "images.txt").exists()
    assert (sparse / "points3D.txt").exists()
    assert (out / "images" / "000001.jpg").exists()
    report = load_json_strict(out / "capture_splat_colmap_summary.json")
    images = sparse / "images.txt"
    assert report["coordinate_contract"]["camera_to_world_conversion"] == (
        "opencv_c2w = arkit_c2w @ diag(1,-1,-1,1)"
    )
    assert report["outputs"]["images"] == {
        "bytes": images.stat().st_size,
        "checksum": f"sha256:{hashlib.sha256(images.read_bytes()).hexdigest()}",
    }


def test_arkit_camera_axes_convert_to_colmap_opencv_axes() -> None:
    angle = math.radians(37.0)
    rotation_y = np.asarray([
        [math.cos(angle), 0.0, math.sin(angle)],
        [0.0, 1.0, 0.0],
        [-math.sin(angle), 0.0, math.cos(angle)],
    ])
    camera_to_world = np.eye(4)
    camera_to_world[:3, :3] = rotation_y
    camera_to_world[:3, 3] = [1.25, -0.4, 2.75]

    quaternion, translation = arkit_camera_to_colmap_pose(camera_to_world)
    rotation_world_to_camera = quaternion_rotation(*quaternion)
    reconstructed = np.eye(4)
    reconstructed[:3, :3] = rotation_world_to_camera.T
    reconstructed[:3, 3] = -rotation_world_to_camera.T @ translation

    expected = camera_to_world @ np.diag([1.0, -1.0, -1.0, 1.0])
    np.testing.assert_allclose(reconstructed, expected, atol=1e-12)
    world_point_in_front = camera_to_world @ np.asarray([0.2, -0.1, -3.0, 1.0])
    opencv_point = rotation_world_to_camera @ world_point_in_front[:3] + translation
    np.testing.assert_allclose(opencv_point, [0.2, 0.1, 3.0], atol=1e-12)


def test_arkit_camera_pose_rejects_reflection() -> None:
    camera_to_world = np.diag([-1.0, 1.0, 1.0, 1.0])

    with pytest.raises(ValueError, match="right-handed"):
        arkit_camera_to_colmap_pose(camera_to_world)


def test_colmap_export_scales_intrinsics_to_rgb_image_size(tmp_path: Path) -> None:
    capture = make_capture(tmp_path / "capture")
    Image.new("RGB", (8, 6), (255, 0, 0)).save(capture / "rgb" / "000001.jpg")
    out = tmp_path / "out"

    export_colmap_text(capture, out)
    ingest_capture(capture, tmp_path / "ingest")

    cameras = (out / "sparse" / "0" / "cameras.txt").read_text(encoding="utf-8")
    transforms = load_json_strict(tmp_path / "ingest" / "nerfstudio_dataset" / "transforms.json")
    assert "1 PINHOLE 8 6 8 6 4 3" in cameras
    assert transforms["w"] == 8
    assert transforms["h"] == 6
    assert transforms["fl_x"] == 8
    assert transforms["fl_y"] == 6


def test_host_exports_only_accepted_quality_frames(tmp_path: Path) -> None:
    capture = tmp_path / "capture"
    image_dir = capture / "rgb"
    image_dir.mkdir(parents=True)
    Image.new("RGB", (4, 4), (255, 0, 0)).save(image_dir / "accepted.jpg")
    Image.new("RGB", (4, 4), (0, 0, 255)).save(image_dir / "rejected.jpg")
    write_json_strict(capture / "capture.json", {
        "schema": "capture_splat.v0.1",
        "intrinsics": {"fl_x": 4.0, "fl_y": 4.0, "cx": 2.0, "cy": 2.0, "w": 4, "h": 4},
        "frames": [
            {
                "rgb": "rgb/accepted.jpg",
                "timestamp": 1.0,
                "transform_matrix": [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]],
                "capture_quality": {"accepted": True, "reason": "useful_keyframe", "score": 0.9},
            },
            {
                "rgb": "rgb/rejected.jpg",
                "timestamp": 2.0,
                "transform_matrix": [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]],
                "capture_quality": {"accepted": False, "reason": "low_blur_score", "score": 0.1},
            },
        ],
    })

    ingest_summary = ingest_capture(capture, tmp_path / "ingest")
    colmap_summary = export_colmap_text(capture, tmp_path / "colmap")
    transforms = load_json_strict(tmp_path / "ingest" / "nerfstudio_dataset" / "transforms.json")

    assert ingest_summary["frame_count"] == 1
    assert ingest_summary["frame_selection"]["excluded_rejected_frames"] == 1
    assert transforms["frames"][0]["source_frame_index"] == 1
    assert colmap_summary["image_count"] == 1
    assert (tmp_path / "colmap" / "images" / "000001.jpg").exists()
    assert not (tmp_path / "colmap" / "images" / "000002.jpg").exists()


def test_host_rejects_capture_with_no_accepted_frames(tmp_path: Path) -> None:
    capture = tmp_path / "capture"
    image_dir = capture / "rgb"
    image_dir.mkdir(parents=True)
    Image.new("RGB", (4, 4), (0, 0, 255)).save(image_dir / "rejected.jpg")
    write_json_strict(capture / "capture.json", {
        "schema": "capture_splat.v0.1",
        "intrinsics": {"fl_x": 4.0, "fl_y": 4.0, "cx": 2.0, "cy": 2.0, "w": 4, "h": 4},
        "frames": [{
            "rgb": "rgb/rejected.jpg",
            "timestamp": 2.0,
            "transform_matrix": [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]],
            "capture_quality": {"accepted": False, "reason": "low_blur_score", "score": 0.1},
        }],
    })

    with pytest.raises(ValueError, match="no accepted frames"):
        ingest_capture(capture, tmp_path / "ingest")
    with pytest.raises(ValueError, match="no accepted frames"):
        export_colmap_text(capture, tmp_path / "colmap")
