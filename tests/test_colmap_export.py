from pathlib import Path
from PIL import Image
import pytest

from tests.test_capture_schema import make_capture
from capture_splat.colmap_export import export_colmap_text
from capture_splat.ingest import ingest_capture
from capture_splat.json_utils import load_json_strict, write_json_strict


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
