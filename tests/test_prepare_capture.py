from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from capture_splat.json_utils import load_json_strict, write_json_strict
from capture_splat.prepare_capture import prepare_capture


def _frame(root: Path, index: int, timestamp: float) -> dict[str, object]:
    rgb = root / "rgb" / f"frame_{index:06d}.jpg"
    depth = root / "depth" / f"depth_{index:06d}.npy"
    confidence = root / "confidence" / f"confidence_{index:06d}.npy"
    rgb.parent.mkdir(parents=True, exist_ok=True)
    depth.parent.mkdir(parents=True, exist_ok=True)
    confidence.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (16, 12), (40 + index, 80, 120)).save(rgb)
    np.save(depth, np.full((6, 8), 1.0, dtype=np.float32), allow_pickle=False)
    np.save(confidence, np.full((6, 8), 2, dtype=np.uint8), allow_pickle=False)
    return {
        "rgb": rgb.relative_to(root).as_posix(),
        "depth": depth.relative_to(root).as_posix(),
        "confidence": confidence.relative_to(root).as_posix(),
        "timestamp": timestamp,
        "transform_matrix": [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]],
        "intrinsics": {"fl_x": 8, "fl_y": 8, "cx": 4, "cy": 3, "w": 8, "h": 6},
        "capture_quality": {
            "accepted": True,
            "reason": "accepted",
            "blur_score": 0.01,
            "parallax_meters": 0.06,
            "colmap_overlap_score": 0.6,
            "valid_depth_ratio": 0.7,
        },
    }


def _capture(root: Path, count: int = 2) -> Path:
    root.mkdir(parents=True)
    frames = [_frame(root, index, 100.0 + index) for index in range(1, count + 1)]
    write_json_strict(root / "capture.json", {
        "schema": "capture_splat.v0.3",
        "capture_profile": "video_3dgs_max",
        "capture_intent": "scene_cluster",
        "frames": frames,
    })
    return root


def test_prepare_capture_keeps_accepted_rgbd_and_holds_without_video(tmp_path: Path) -> None:
    capture = _capture(tmp_path / "capture")

    summary = prepare_capture(capture, tmp_path / "prepared", target_frames=3)
    manifest = load_json_strict(tmp_path / "prepared/frames/capture.json")

    assert summary["decision"] == "hold"
    assert summary["accepted_rgbd_frames"] == 2
    assert summary["continuous_video_supplements"] == 0
    assert "continuous_video_or_frame_index_missing" in summary["warnings"]
    assert summary["sfm_request"]["matcher"] == "exhaustive"
    assert len(manifest["frames"]) == 2
    assert all(frame["source_kind"] == "accepted_rgbd" for frame in manifest["frames"])
    assert (tmp_path / "prepared/frames/depth/000001.npy").exists()


def test_prepare_capture_refuses_non_empty_output(tmp_path: Path) -> None:
    capture = _capture(tmp_path / "capture")
    output = tmp_path / "prepared"
    output.mkdir()
    (output / "stale.txt").write_text("stale", encoding="utf-8")

    with pytest.raises(FileExistsError, match="not empty"):
        prepare_capture(capture, output, target_frames=1)


def test_prepare_capture_deduplicates_video_on_ar_clock(tmp_path: Path, monkeypatch) -> None:
    capture = _capture(tmp_path / "capture", count=1)
    (capture / "video").mkdir()
    (capture / "video/capture.mov").write_bytes(b"video")
    (capture / "metadata").mkdir()
    (capture / "metadata/frame_index.jsonl").write_text("{}\n", encoding="utf-8")

    def fake_extract(video: Path, out_dir: Path, **kwargs):
        frames = []
        for index, timestamp in enumerate((101.04, 102.0), start=1):
            image = out_dir / "images" / f"{index:06d}.jpg"
            image.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (16, 12), (index * 30, 80, 120)).save(image)
            frames.append({
                "rgb": image.relative_to(out_dir).as_posix(),
                "timestamp": timestamp,
                "video_timestamp": timestamp - 100,
                "timestamp_domain": "ar_session",
                "transform_matrix": [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]],
                "intrinsics": {"fl_x": 8, "fl_y": 8, "cx": 4, "cy": 3, "w": 8, "h": 6},
                "accepted": True,
            })
        write_json_strict(out_dir / "capture.json", {"schema": "capture_splat.v0.2", "frames": frames})
        return {"extracted_frames": 2}

    monkeypatch.setattr("capture_splat.prepare_capture.run_extract_frames", fake_extract)
    summary = prepare_capture(capture, tmp_path / "prepared", target_frames=2)
    manifest = load_json_strict(tmp_path / "prepared/frames/capture.json")

    assert summary["continuous_video_supplements"] == 1
    assert [frame["timestamp"] for frame in manifest["frames"]] == [101.0, 102.0]


def test_prepare_capture_uses_actual_frame_count_for_retrieval(tmp_path: Path) -> None:
    capture = _capture(tmp_path / "capture", count=251)

    summary = prepare_capture(capture, tmp_path / "prepared", target_frames=251)

    assert summary["sfm_request"]["matcher"] == "retrieval"
    assert summary["sfm_request"]["features"] == "hloc"


def test_prepare_capture_writes_non_destructive_object_mask(tmp_path: Path) -> None:
    capture = _capture(tmp_path / "capture", count=1)
    (capture / "metadata").mkdir()
    write_json_strict(capture / "metadata/object_matte_report.json", {
        "frame_records": [{
            "rgb": "rgb/frame_000001.jpg",
            "timestamp": 101.0,
            "depth_support": {
                "depth_bbox_px": {"x0": 1, "y0": 1, "x1": 7, "y1": 5},
                "depth_band_meters": {"min": 0.8, "max": 1.2},
            },
        }],
    })

    summary = prepare_capture(capture, tmp_path / "prepared", target_frames=1)
    manifest = load_json_strict(tmp_path / "prepared/frames/capture.json")

    assert summary["copied_sidecars"]["object_mask"] == 1
    assert manifest["frames"][0]["object_mask"] == "masks/object/000001.png"
    assert (capture / "rgb/frame_000001.jpg").exists()
