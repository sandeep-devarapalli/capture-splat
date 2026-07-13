import json
from pathlib import Path
from PIL import Image

from capture_splat.capture_schema import CAPTURE_SCHEMA, load_capture
from capture_splat.json_utils import write_json_strict


def make_capture(root: Path) -> Path:
    image_dir = root / "rgb"
    image_dir.mkdir(parents=True)
    Image.new("RGB", (4, 4), (255, 0, 0)).save(image_dir / "000001.jpg")
    write_json_strict(root / "capture.json", {
        "schema": CAPTURE_SCHEMA,
        "intrinsics": {"fl_x": 4.0, "fl_y": 4.0, "cx": 2.0, "cy": 2.0, "w": 4, "h": 4},
        "frames": [{
            "rgb": "rgb/000001.jpg",
            "timestamp": 1.0,
            "transform_matrix": [[1,0,0,0],[0,1,0,0],[0,0,1,0],[0,0,0,1]],
        }],
    })
    return root


def test_load_capture(tmp_path: Path) -> None:
    capture = make_capture(tmp_path)
    data = load_capture(capture)
    assert data["schema"] == CAPTURE_SCHEMA
    assert len(data["frames"]) == 1


def test_load_capture_v03_with_capture_policy_sidecars(tmp_path: Path) -> None:
    capture = make_capture(tmp_path)
    manifest = capture / "capture.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data.update({
        "schema": "capture_splat.v0.3",
        "capture_policy_file": "metadata/capture_policy.json",
        "sensor_capabilities_file": "metadata/sensor_capabilities.json",
        "finalization_report_file": "metadata/finalization_report.json",
        "spatial_guidance_report_file": "metadata/spatial_guidance_report.json",
    })
    write_json_strict(manifest, data)

    loaded = load_capture(capture)

    assert loaded["schema"] == "capture_splat.v0.3"
    assert loaded["capture_policy_file"] == "metadata/capture_policy.json"
    assert loaded["spatial_guidance_report_file"] == "metadata/spatial_guidance_report.json"
