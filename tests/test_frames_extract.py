import json
import shutil
import subprocess
from pathlib import Path

import pytest

from capture_splat.capture_schema import load_capture
from capture_splat.frames_extract import match_frame_index, pick_sharpest_indices, run_extract_frames
from capture_splat.json_utils import load_json_strict

ffmpeg_missing = shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None


def make_video(path: Path, frames: int = 12, rate: int = 12) -> None:
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", f"testsrc=duration={frames / rate}:size=128x96:rate={rate}",
            "-pix_fmt", "yuv420p", str(path),
        ],
        check=True,
    )


def test_pick_sharpest_indices_selects_window_maxima() -> None:
    sharpness = [1.0, 5.0, 2.0, 9.0, 1.0, 0.5, 3.0]
    assert pick_sharpest_indices(sharpness, 3) == [1, 3, 6]


def test_match_frame_index_by_timestamp(tmp_path: Path) -> None:
    index = tmp_path / "frame_index.jsonl"
    entries = [{"timestamp": frame / 12, "camera_to_world": [[1, 0, 0, frame]], "intrinsics": {"fx": 10, "fy": 10, "cx": 5, "cy": 5}} for frame in range(12)]
    index.write_text("\n".join(json.dumps(entry) for entry in entries), encoding="utf-8")

    matches, matched = match_frame_index([0, 6, 11], 12.0, index)

    assert matched == 3
    assert matches[1]["camera_to_world"][0][3] == 6


@pytest.mark.skipif(ffmpeg_missing, reason="ffmpeg not available")
def test_extract_frames_interval_and_summary(tmp_path: Path) -> None:
    video = tmp_path / "clip.mp4"
    make_video(video, frames=12, rate=12)

    summary = run_extract_frames(video, tmp_path / "out", target_frames=4, pick="first")

    assert summary["interval"] == 3
    assert summary["extracted_frames"] == 4
    assert summary["picked_video_frames"] == [0, 3, 6, 9]
    assert (tmp_path / "out" / "images" / "000001.jpg").exists()
    saved = load_json_strict(tmp_path / "out" / "capture_splat_frames_summary.json")
    assert saved["authority"]["quality_claim"] is False


@pytest.mark.skipif(ffmpeg_missing, reason="ffmpeg not available")
def test_extract_frames_sharpest_pick_records_scores(tmp_path: Path) -> None:
    video = tmp_path / "clip.mp4"
    make_video(video, frames=12, rate=12)

    summary = run_extract_frames(video, tmp_path / "out", target_frames=4, pick="sharpest")

    assert summary["pick"] == "sharpest"
    assert summary["extracted_frames"] == 4
    assert "sharpness" in summary
    assert len(summary["picked_video_frames"]) == 4


@pytest.mark.skipif(ffmpeg_missing, reason="ffmpeg not available")
def test_extract_frames_attaches_poses_from_frame_index(tmp_path: Path) -> None:
    video = tmp_path / "clip.mp4"
    make_video(video, frames=12, rate=12)
    index = tmp_path / "frame_index.jsonl"
    identity = [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]
    entries = [
        {"timestamp": frame / 12, "camera_to_world": identity, "intrinsics": {"fx": 100, "fy": 100, "cx": 64, "cy": 48, "w": 128, "h": 96}, "tracking_state": "normal"}
        for frame in range(12)
    ]
    index.write_text("\n".join(json.dumps(entry) for entry in entries), encoding="utf-8")

    summary = run_extract_frames(video, tmp_path / "out", target_frames=4, pick="first", frame_index=index)

    assert summary["pose_attachment"] == "matched_4_of_4"
    assert summary["capture_manifest_frames"] == 4
    capture = load_capture(tmp_path / "out")
    assert capture["schema"] == "capture_splat.v0.2"
    assert capture["frames"][0]["intrinsics"]["fl_x"] == 100.0
