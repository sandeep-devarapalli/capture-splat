from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from .json_utils import write_json_strict

SUMMARY_SCHEMA = "capture_splat.frames_extract_summary.v0.1"
CAPTURE_SCHEMA_V02 = "capture_splat.v0.2"


def probe_video(video: Path) -> tuple[int, float]:
    completed = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets",
            "-show_entries", "stream=nb_read_packets,avg_frame_rate", "-of", "json", str(video),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    stream = json.loads(completed.stdout)["streams"][0]
    total = int(stream["nb_read_packets"])
    numerator, _, denominator = str(stream.get("avg_frame_rate", "30/1")).partition("/")
    fps = float(numerator) / float(denominator or 1) if float(denominator or 1) else 30.0
    return total, fps


def laplacian_variance(image: Image.Image) -> float:
    gray = np.asarray(image.convert("L"), dtype=np.float32)
    if gray.shape[0] < 3 or gray.shape[1] < 3:
        return 0.0
    lap = 4 * gray[1:-1, 1:-1] - gray[:-2, 1:-1] - gray[2:, 1:-1] - gray[1:-1, :-2] - gray[1:-1, 2:]
    return float(lap.var())


def pick_sharpest_indices(sharpness: list[float], interval: int) -> list[int]:
    picks = []
    for start in range(0, len(sharpness), interval):
        window = sharpness[start:start + interval]
        picks.append(start + max(range(len(window)), key=lambda offset: window[offset]))
    return picks


def match_frame_index(picked: list[int], fps: float, frame_index_path: Path) -> tuple[list[dict[str, Any] | None], int]:
    entries: list[dict[str, Any]] = []
    for line in frame_index_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        entries.append(json.loads(line))
    timestamps = np.asarray([float(entry["timestamp"]) for entry in entries])
    tolerance = 0.75 / fps if fps > 0 else 0.05
    matches: list[dict[str, Any] | None] = []
    matched = 0
    for frame_number in picked:
        target = frame_number / fps if fps > 0 else 0.0
        position = int(np.argmin(np.abs(timestamps - target))) if len(timestamps) else -1
        if position >= 0 and abs(float(timestamps[position]) - target) <= tolerance:
            matches.append(entries[position])
            matched += 1
        else:
            matches.append(None)
    return matches, matched


def intrinsics_from_entry(entry: dict[str, Any], width: int, height: int) -> dict[str, float] | None:
    intr = entry.get("intrinsics")
    if not isinstance(intr, dict):
        return None
    fx = intr.get("fl_x", intr.get("fx"))
    fy = intr.get("fl_y", intr.get("fy"))
    cx = intr.get("cx")
    cy = intr.get("cy")
    if fx is None or fy is None or cx is None or cy is None:
        return None
    source_w = float(intr.get("w", intr.get("width", width)))
    source_h = float(intr.get("h", intr.get("height", height)))
    x_scale = width / source_w if source_w else 1.0
    y_scale = height / source_h if source_h else 1.0
    return {
        "fl_x": float(fx) * x_scale,
        "fl_y": float(fy) * y_scale,
        "cx": float(cx) * x_scale,
        "cy": float(cy) * y_scale,
        "w": float(width),
        "h": float(height),
    }


def extract_selected_frames(video: Path, picked: list[int], out_dir: Path, max_edge: int) -> list[Path]:
    select_expr = "+".join(f"eq(n\\,{frame})" for frame in picked)
    scale = f"scale=w='min({max_edge},iw)':h='min({max_edge},ih)':force_original_aspect_ratio=decrease"
    out_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error", "-i", str(video),
            "-vf", f"select='{select_expr}',{scale}", "-vsync", "vfr",
            "-q:v", "2", str(out_dir / "%06d.jpg"),
        ],
        check=True,
        text=True,
    )
    return sorted(out_dir.glob("*.jpg"))


def run_extract_frames(
    video: Path,
    out_dir: Path,
    target_frames: int = 300,
    max_edge: int = 1920,
    pick: str = "sharpest",
    frame_index: Path | None = None,
) -> dict[str, Any]:
    video = video.resolve()
    out_dir = out_dir.resolve()
    if not video.exists():
        raise FileNotFoundError(f"video missing: {video}")
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        raise RuntimeError("ffmpeg/ffprobe are required for extract-frames")
    target_frames = max(1, min(int(target_frames), 600))
    total, fps = probe_video(video)
    interval = max(1, math.ceil(total / target_frames))
    sharpness: list[float] = []
    if pick == "sharpest" and interval > 1:
        with tempfile.TemporaryDirectory(prefix="capture_splat_frames_") as temp:
            temp_dir = Path(temp)
            subprocess.run(
                [
                    "ffmpeg", "-y", "-v", "error", "-i", str(video),
                    "-vf", "scale=w='min(320,iw)':h='min(320,ih)':force_original_aspect_ratio=decrease",
                    "-q:v", "5", str(temp_dir / "%06d.jpg"),
                ],
                check=True,
                text=True,
            )
            small = sorted(temp_dir.glob("*.jpg"))
            for path in small:
                with Image.open(path) as image:
                    sharpness.append(laplacian_variance(image))
        picked = pick_sharpest_indices(sharpness, interval)
    else:
        picked = list(range(0, total, interval))
    images_dir = out_dir / "images"
    written = extract_selected_frames(video, picked, images_dir, max_edge)
    if len(written) != len(picked):
        raise RuntimeError(f"expected {len(picked)} frames, ffmpeg wrote {len(written)}")

    summary: dict[str, Any] = {
        "schema": SUMMARY_SCHEMA,
        "video": str(video),
        "output_dir": str(out_dir),
        "total_video_frames": total,
        "fps": fps,
        "target_frames": target_frames,
        "interval": interval,
        "pick": pick if interval > 1 else "first",
        "extracted_frames": len(written),
        "picked_video_frames": picked,
        "max_edge": max_edge,
        "pose_attachment": "not_requested",
        "authority": {"frame_selection_evidence": True, "quality_claim": False},
    }
    if sharpness:
        picked_scores = [sharpness[index] for index in picked]
        summary["sharpness"] = {
            "mean_all": float(np.mean(sharpness)),
            "mean_picked": float(np.mean(picked_scores)),
            "min_picked": float(np.min(picked_scores)),
        }
    if frame_index is not None:
        matches, matched = match_frame_index(picked, fps, frame_index)
        summary["pose_attachment"] = f"matched_{matched}_of_{len(picked)}"
        frames: list[dict[str, Any]] = []
        for path, frame_number, entry in zip(written, picked, matches):
            if entry is None:
                continue
            with Image.open(path) as image:
                width, height = image.size
            intrinsics = intrinsics_from_entry(entry, width, height)
            transform = entry.get("camera_to_world", entry.get("transform_matrix"))
            if intrinsics is None or transform is None:
                continue
            frames.append({
                "rgb": path.relative_to(out_dir).as_posix(),
                "transform_matrix": transform,
                "intrinsics": intrinsics,
                "timestamp": float(entry["timestamp"]),
                "accepted": True,
                "source_video_frame": frame_number,
                "tracking_state": entry.get("tracking_state"),
            })
        if frames:
            manifest = {
                "schema": CAPTURE_SCHEMA_V02,
                "source": "capture_splat.extract_frames",
                "video": video.name,
                "frames": frames,
                "authority": {"pose_prior": "device_frame_index", "quality_claim": False},
            }
            write_json_strict(out_dir / "capture.json", manifest)
            summary["capture_manifest"] = "capture.json"
            summary["capture_manifest_frames"] = len(frames)
    write_json_strict(out_dir / "capture_splat_frames_summary.json", summary)
    return summary


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extract training frames from a capture video.")
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--target-frames", type=int, default=300)
    parser.add_argument("--max-edge", type=int, default=1920)
    parser.add_argument("--pick", choices=["sharpest", "first"], default="sharpest")
    parser.add_argument("--frame-index", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    summary = run_extract_frames(
        args.video,
        args.out,
        target_frames=args.target_frames,
        max_edge=args.max_edge,
        pick=args.pick,
        frame_index=args.frame_index,
    )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
