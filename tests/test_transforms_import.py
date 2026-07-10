from pathlib import Path

import numpy as np
from PIL import Image

from capture_splat.capture_schema import load_capture
from capture_splat.ingest import ingest_capture
from capture_splat.json_utils import load_json_strict, write_json_strict
from capture_splat.transforms_import import import_transforms_package


def make_transforms_export(root: Path, include_depth: bool = True) -> Path:
    image_dir = root / "images"
    depth_dir = root / "depth"
    image_dir.mkdir(parents=True)
    depth_dir.mkdir()
    Image.new("RGB", (8, 6), (255, 0, 0)).save(image_dir / "frame_0001.jpg")
    frame = {
        "file_path": "images/frame_0001.jpg",
        "transform_matrix": [[1, 0, 0, 0.1], [0, 1, 0, 0.2], [0, 0, 1, 0.3], [0, 0, 0, 1]],
        "timestamp": 1.5,
    }
    if include_depth:
        np.save(depth_dir / "frame_0001.npy", np.ones((3, 4), dtype=np.float32))
        frame["depth_file_path"] = "depth/frame_0001.npy"
    write_json_strict(root / "transforms.json", {
        "camera_model": "OPENCV",
        "k1": 0.01,
        "k2": -0.02,
        "p1": 0.001,
        "p2": -0.001,
        "fl_x": 6.0,
        "fl_y": 6.0,
        "cx": 4.0,
        "cy": 3.0,
        "w": 8,
        "h": 6,
        "frames": [frame],
    })
    return root


def test_import_transforms_writes_capture_package(tmp_path: Path) -> None:
    source = make_transforms_export(tmp_path / "source")
    out = tmp_path / "capture"

    summary = import_transforms_package(source, out)
    capture = load_capture(out)

    assert summary["frame_count"] == 1
    assert summary["depth_frame_count"] == 1
    assert capture["frames"][0]["rgb"] == "rgb/000001.jpg"
    assert capture["frames"][0]["depth"] == "depth/000001.npy"
    assert capture["frames"][0]["capture_quality"]["reason"] == "imported_transforms"
    assert capture["frames"][0]["intrinsics"]["camera_model"] == "OPENCV"
    assert capture["frames"][0]["intrinsics"]["k2"] == -0.02


def test_import_transforms_feeds_existing_ingest(tmp_path: Path) -> None:
    source = make_transforms_export(tmp_path / "source")
    capture_dir = tmp_path / "capture"
    import_transforms_package(source, capture_dir)

    summary = ingest_capture(capture_dir, tmp_path / "ingest")
    transforms = load_json_strict(tmp_path / "ingest" / "nerfstudio_dataset" / "transforms.json")

    assert summary["frame_count"] == 1
    assert transforms["frames"][0]["source_frame_index"] == 1


def test_import_transforms_can_warn_on_missing_depth(tmp_path: Path) -> None:
    source = make_transforms_export(tmp_path / "source", include_depth=False)

    summary = import_transforms_package(source, tmp_path / "capture")

    assert summary["depth_frame_count"] == 0
    assert summary["warnings"] == ["frame_000001_depth_missing"]
