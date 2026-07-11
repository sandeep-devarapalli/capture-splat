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
                "photometric": {"exposure_duration": 0.02, "iso": 100.0, "lens_position": 0.6},
            })
        write_json_strict(out_dir / "capture.json", {"schema": "capture_splat.v0.2", "frames": frames})
        return {"extracted_frames": 2}

    monkeypatch.setattr("capture_splat.prepare_capture.run_extract_frames", fake_extract)
    summary = prepare_capture(capture, tmp_path / "prepared", target_frames=2)
    manifest = load_json_strict(tmp_path / "prepared/frames/capture.json")

    assert summary["continuous_video_supplements"] == 1
    assert [frame["timestamp"] for frame in manifest["frames"]] == [101.0, 102.0]
    assert manifest["frames"][1]["photometric"]["lens_position"] == 0.6


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

    summary = prepare_capture(capture, tmp_path / "prepared", recipe="object", target_frames=1)
    manifest = load_json_strict(tmp_path / "prepared/frames/capture.json")

    assert summary["copied_sidecars"]["object_mask"] == 1
    assert manifest["frames"][0]["object_mask"] == "masks/object/000001.png"
    assert manifest["frames"][0]["valid_mask"] == "masks/valid/000001.jpg.png"
    valid = np.asarray(Image.open(tmp_path / "prepared/frames/masks/valid/000001.jpg.png"))
    assert set(np.unique(valid)).issubset({0, 255})
    assert summary["valid_masks"]["semantics"] == "white_valid_for_features_and_training"
    assert summary["camera_evidence"]["complete"] is True
    assert (capture / "rgb/frame_000001.jpg").exists()


def test_prepare_capture_recenters_legacy_object_depth_band_from_projection(tmp_path: Path) -> None:
    capture = _capture(tmp_path / "capture", count=1)
    np.save(capture / "depth/depth_000001.npy", np.full((6, 8), 2.0, dtype=np.float32), allow_pickle=False)
    (capture / "metadata").mkdir()
    write_json_strict(capture / "metadata/object_matte_report.json", {
        "frame_records": [{
            "rgb": "rgb/frame_000001.jpg",
            "timestamp": 101.0,
            "projection": {"optical_depth_meters": 2.0},
            "depth_support": {
                "depth_bbox_px": {"x0": 1, "y0": 1, "x1": 7, "y1": 5},
                "depth_band_meters": {"min": 0.8, "max": 1.2},
            },
        }],
    })

    summary = prepare_capture(capture, tmp_path / "prepared", recipe="object", target_frames=1)

    assert summary["copied_sidecars"]["object_mask"] == 1
    assert summary["valid_masks"]["missing_frames"] == []


def test_prepare_capture_does_not_pad_object_recipe_with_unmasked_video(tmp_path: Path, monkeypatch) -> None:
    capture = _capture(tmp_path / "capture", count=1)
    (capture / "video").mkdir()
    (capture / "video/capture.mov").write_bytes(b"video")
    (capture / "metadata").mkdir()
    (capture / "metadata/frame_index.jsonl").write_text("{}\n", encoding="utf-8")
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

    def unexpected_extract(*args, **kwargs):
        raise AssertionError("strict object preparation must not extract unmasked video supplements")

    monkeypatch.setattr("capture_splat.prepare_capture.run_extract_frames", unexpected_extract)
    summary = prepare_capture(capture, tmp_path / "prepared", recipe="object", target_frames=3)

    assert summary["requested_target_frames"] == 3
    assert summary["target_frames"] == 1
    assert summary["continuous_video_supplements"] == 0
    assert summary["prepared_frames"] == 1


def test_prepare_capture_downselects_temporal_bins_by_parallax(tmp_path: Path) -> None:
    capture = _capture(tmp_path / "capture", count=6)
    manifest = load_json_strict(capture / "capture.json")
    for frame, parallax in zip(manifest["frames"], (0.01, 0.08, 0.02, 0.09, 0.01, 0.10), strict=True):
        frame["capture_quality"]["parallax_meters"] = parallax
    write_json_strict(capture / "capture.json", manifest)

    summary = prepare_capture(capture, tmp_path / "prepared", recipe="desk", target_frames=3)
    prepared = load_json_strict(tmp_path / "prepared/frames/capture.json")

    assert summary["selection"] == "temporal_bins_ranked_by_parallax_blur_features"
    assert [frame["source_frame_index"] for frame in prepared["frames"]] == [2, 4, 6]


def test_prepare_capture_room_masks_cover_every_frame_with_white_valid_semantics(tmp_path: Path) -> None:
    capture = _capture(tmp_path / "capture", count=2)

    summary = prepare_capture(capture, tmp_path / "prepared", recipe="room", target_frames=2)

    assert summary["copied_sidecars"]["valid_mask"] == 2
    for index in (1, 2):
        valid = np.asarray(Image.open(tmp_path / f"prepared/frames/masks/valid/{index:06d}.jpg.png"))
        assert np.all(valid == 255)


def test_prepare_capture_desk_uses_full_scene_when_object_support_is_missing(tmp_path: Path) -> None:
    capture = _capture(tmp_path / "capture", count=1)

    summary = prepare_capture(capture, tmp_path / "prepared", recipe="desk", target_frames=1)
    manifest = load_json_strict(tmp_path / "prepared/frames/capture.json")

    assert summary["copied_sidecars"]["valid_mask"] == 1
    assert summary["decision"] == "hold"
    assert "object_support_masks_incomplete" not in summary["warnings"]
    assert summary["valid_masks"]["missing_frames"] == []
    assert manifest["frames"][0]["valid_mask"] == "masks/valid/000001.jpg.png"


def test_prepare_capture_enriches_partial_photometric_metadata(tmp_path: Path) -> None:
    capture = _capture(tmp_path / "capture", count=1)
    manifest = load_json_strict(capture / "capture.json")
    manifest["frames"][0]["photometric"] = {"exposure_duration": 0.02}
    write_json_strict(capture / "capture.json", manifest)
    (capture / "metadata").mkdir()
    (capture / "metadata/frame_index.jsonl").write_text(
        '{"ar_timestamp":101.0,"exposure_duration":0.03,"iso":125.0,"lens_position":0.7}\n',
        encoding="utf-8",
    )

    prepare_capture(capture, tmp_path / "prepared", recipe="room", target_frames=1)
    prepared = load_json_strict(tmp_path / "prepared/frames/capture.json")

    assert prepared["frames"][0]["photometric"]["exposure_duration"] == 0.02
    assert prepared["frames"][0]["photometric"]["iso"] == 125.0
    assert prepared["frames"][0]["photometric"]["lens_position"] == 0.7


def test_prepare_capture_reports_person_mask_resizing(tmp_path: Path) -> None:
    capture = _capture(tmp_path / "capture", count=1)
    person = capture / "masks/person/frame_000001.png"
    person.parent.mkdir(parents=True)
    Image.new("L", (8, 6), 0).save(person)
    manifest = load_json_strict(capture / "capture.json")
    manifest["frames"][0]["person_mask"] = "masks/person/frame_000001.png"
    write_json_strict(capture / "capture.json", manifest)

    summary = prepare_capture(capture, tmp_path / "prepared", recipe="room", target_frames=1)

    resized = summary["valid_masks"]["records"][0]["resized_sources"]
    assert resized == [{"source": "person", "from": [8, 6], "to": [16, 12]}]
