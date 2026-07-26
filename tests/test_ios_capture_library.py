import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="Swift host probe requires macOS")


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_capture_library_classifies_saved_bundles_and_confines_paths(tmp_path: Path) -> None:
    ready = tmp_path / "capture_splat_ready"
    partial = tmp_path / "capture_splat_partial"
    escaped = tmp_path / "capture_splat_escaped"

    _write_json(
        ready / "capture.json",
        {
            "capture_intent": "scene_cluster",
            "frames": [{}, {}],
            "finalization_report_file": "metadata/finalization_report.json",
            "pointcloud_preview_file": "pointcloud_preview/preview.json",
            "room_plan_file": "room_plan/room.usdz",
        },
    )
    _write_json(
        ready / "metadata/finalization_report.json",
        {
            "status": "finalized",
            "manifest_written": True,
            "accepted_keyframe_count": 2,
        },
    )
    _write_json(ready / "pointcloud_preview/preview.json", {"point_count": 42})
    (ready / "room_plan").mkdir(parents=True)
    (ready / "room_plan/room.usdz").write_bytes(b"usdz")

    _write_json(partial / "capture.json", {"frames": [{}]})
    _write_json(
        escaped / "capture.json",
        {
            "frames": [],
            "finalization_report_file": "../outside.json",
        },
    )

    repository = Path(__file__).resolve().parents[1]
    executable = tmp_path / "capture-library-probe"
    environment = os.environ.copy()
    environment["CLANG_MODULE_CACHE_PATH"] = str(tmp_path / "clang-module-cache")
    environment["SWIFT_MODULECACHE_PATH"] = str(tmp_path / "swift-module-cache")
    subprocess.run(
        [
            "xcrun",
            "swiftc",
            str(
                repository
                / "apps/ios/CaptureSplat/CaptureSplat/Sources/CaptureLibrary.swift"
            ),
            str(repository / "tests/swift/CaptureLibraryProbe.swift"),
            "-o",
            str(executable),
        ],
        check=True,
        env=environment,
    )
    result = subprocess.run(
        [str(executable), str(tmp_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    records = {record["name"]: record for record in json.loads(result.stdout)}

    assert records["capture_splat_ready"] == {
        "detail": "Finalized capture bundle.",
        "frame_count": 2,
        "has_preview": True,
        "has_room_plan": True,
        "name": "capture_splat_ready",
        "point_count": 42,
        "state": "ready",
    }
    assert records["capture_splat_partial"]["state"] == "partial", records["capture_splat_partial"]
    assert records["capture_splat_partial"]["has_preview"] is False
    assert records["capture_splat_escaped"]["state"] == "invalid"
    assert "escapes" in records["capture_splat_escaped"]["detail"]
