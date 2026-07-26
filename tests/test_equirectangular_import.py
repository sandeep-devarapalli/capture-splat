from pathlib import Path

import numpy as np
from PIL import Image
import pytest

from capture_splat.equirectangular_import import (
    default_virtual_views,
    import_equirectangular,
    project_equirectangular,
    virtual_camera_rotation,
)
from capture_splat.json_utils import load_json_strict


def write_panorama(path: Path, width: int = 360, height: int = 180) -> None:
    pixels = np.zeros((height, width, 3), dtype=np.uint8)
    pixels[:, 150:210] = [240, 30, 20]
    pixels[:, 240:300] = [20, 220, 40]
    Image.fromarray(pixels).save(path)


def test_virtual_camera_rotation_uses_documented_axes() -> None:
    assert np.allclose(virtual_camera_rotation(0, 0), np.eye(3), atol=1e-12)
    assert np.allclose(virtual_camera_rotation(90, 0)[:, 2], [1, 0, 0], atol=1e-12)
    assert virtual_camera_rotation(0, 45)[1, 2] > 0
    assert len(default_virtual_views()) == 14


def test_projection_centers_requested_yaw() -> None:
    pixels = np.zeros((180, 360, 3), dtype=np.uint8)
    pixels[:, 150:210] = [240, 30, 20]
    pixels[:, 240:300] = [20, 220, 40]
    panorama = Image.fromarray(pixels)

    forward, _ = project_equirectangular(panorama, 0, 0, 33, 90)
    right, _ = project_equirectangular(panorama, 90, 0, 33, 90)

    assert np.asarray(forward)[16, 16].tolist() == [240, 30, 20]
    assert np.asarray(right)[16, 16].tolist() == [20, 220, 40]


def test_import_360_writes_virtual_rig_without_fake_capture_poses(tmp_path: Path) -> None:
    panorama = tmp_path / "room.png"
    write_panorama(panorama)
    out = tmp_path / "out"

    summary = import_equirectangular(panorama, out, size=32, fov_degrees=100)

    assert summary["decision"] == "ready"
    assert summary["panorama_count"] == 1
    assert summary["projection_count"] == 14
    assert len(list((out / "images").glob("*.png"))) == 14
    assert len(list((out / "masks/valid").glob("*.png"))) == 14
    assert not (out / "capture.json").exists()
    rig = load_json_strict(out / "metadata/equirectangular_rig.json")
    assert rig["schema"] == "capture_splat.equirectangular_rig.v0.1"
    assert rig["authority"]["recovered_world_poses"] is False
    assert rig["source_panoramas"][0]["checksum"].startswith("sha256:")
    assert all(not Path(view["image"]).is_absolute() for view in rig["virtual_views"])
    assert all(not Path(view["valid_mask"]).is_absolute() for view in rig["virtual_views"])
    assert all(view["image_evidence"]["checksum"].startswith("sha256:") for view in rig["virtual_views"])
    first_mask = Image.open(out / rig["virtual_views"][0]["valid_mask"])
    assert set(np.asarray(first_mask).ravel()) == {255}


def test_import_360_rejects_non_equirectangular_input_and_dirty_output(tmp_path: Path) -> None:
    panorama = tmp_path / "bad.png"
    Image.new("RGB", (100, 100), "white").save(panorama)

    with pytest.raises(ValueError, match="2:1"):
        import_equirectangular(panorama, tmp_path / "out", size=32)

    good = tmp_path / "good.png"
    write_panorama(good)
    dirty = tmp_path / "dirty"
    dirty.mkdir()
    (dirty / "existing").write_text("keep", encoding="utf-8")
    with pytest.raises(FileExistsError, match="not empty"):
        import_equirectangular(good, dirty, size=32)
