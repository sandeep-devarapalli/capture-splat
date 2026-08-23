from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from capture_splat import cli, portal_route_derivation
from capture_splat.portal_route_derivation import (
    REPORT_NAME,
    derive_portal_route_evidence,
)


def _pose(x: float) -> list[list[float]]:
    return [
        [1.0, 0.0, 0.0, x],
        [0.0, 1.0, 0.0, 1.0],
        [0.0, 0.0, 1.0, 0.0],
        [0.0, 0.0, 0.0, 1.0],
    ]


def _intrinsics() -> dict[str, float]:
    return {"fl_x": 100.0, "fl_y": 100.0, "cx": 50.0, "cy": 40.0, "w": 100.0, "h": 80.0}


def _door(portal_id: str, x: float, z: float = 0.0) -> dict[str, object]:
    return {
        "id": portal_id,
        "dimensions_meters": {"x": 1.0, "y": 2.2, "z": 0.1},
        "transform_matrix": [
            [0.0, 0.0, 1.0, x],
            [0.0, 1.0, 0.0, 1.0],
            [-1.0, 0.0, 0.0, z],
            [0.0, 0.0, 0.0, 1.0],
        ],
    }


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, allow_nan=False) + "\n", encoding="utf-8")


def _fixture(tmp_path: Path) -> Path:
    root = tmp_path / "prepared"
    assets = [
        "images/video_000000.jpg",
        "images/video_000001.jpg",
        "images/frame_000001.jpg",
        "depth/depth_000001.npy",
        "confidence/confidence_000001.npy",
        "room_plan/room.usdz",
        "room_plan/room_plan_report.json",
    ]
    for relative in assets:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(relative.encode("utf-8"))

    source = {
        "schema": "capture_splat.v0.3",
        "frame_index_file": "metadata/frame_index.jsonl",
        "video_frame_count": 2,
        "frames": [
            {
                "timestamp": 3.0,
                "transform_matrix": _pose(0.5),
                "intrinsics": _intrinsics(),
                "capture_quality": {"accepted": True},
            }
        ],
    }
    trajectory = [
        {
            "video_frame_idx": 0,
            "ar_timestamp": 1.0,
            "camera_to_world": _pose(-0.1),
            "intrinsics": _intrinsics(),
            "tracking_state": "normal",
        },
        {
            "video_frame_idx": 1,
            "ar_timestamp": 1.1,
            "camera_to_world": _pose(0.1),
            "intrinsics": _intrinsics(),
            "tracking_state": "normal",
        },
    ]
    prepared = {
        "schema": "capture_splat.v0.3",
        "source": "capture_splat.prepare_capture",
        "source_capture_manifest_file": "metadata/source_capture.json",
        "frame_index_file": "metadata/frame_index.jsonl",
        "room_plan_semantics_file": "room_plan/room_semantics.json",
        "room_plan_file": "room_plan/room.usdz",
        "room_plan_report_file": "room_plan/room_plan_report.json",
        "frames": [
            {
                "accepted": True,
                "source_kind": "continuous_video",
                "source_video_frame": 0,
                "timestamp": 1.0,
                "transform_matrix": _pose(-0.1),
                "intrinsics": _intrinsics(),
                "rgb": "images/video_000000.jpg",
            },
            {
                "accepted": True,
                "source_kind": "continuous_video",
                "source_video_frame": 1,
                "timestamp": 1.1,
                "transform_matrix": _pose(0.1),
                "intrinsics": _intrinsics(),
                "rgb": "images/video_000001.jpg",
            },
            {
                "accepted": True,
                "source_kind": "accepted_rgbd",
                "source_frame_index": 1,
                "timestamp": 3.0,
                "transform_matrix": _pose(0.5),
                "intrinsics": _intrinsics(),
                "rgb": "images/frame_000001.jpg",
                "depth": "depth/depth_000001.npy",
                "confidence": "confidence/confidence_000001.npy",
            },
        ],
    }
    _write_json(root / "metadata/source_capture.json", source)
    trajectory_path = root / "metadata/frame_index.jsonl"
    trajectory_path.write_text(
        "".join(json.dumps(value, allow_nan=False) + "\n" for value in trajectory),
        encoding="utf-8",
    )
    _write_json(
        root / "room_plan/room_semantics.json",
        {
            "schema": "capture_splat.room_semantics.v0.1",
            "authority": {
                "room_semantic_proposal": True,
                "metric_authority": False,
                "collision_geometry": False,
                "planning_authority": False,
                "semantic_authority": False,
            },
            "doors": [_door("door_0", 0.0, 10.0), _door("door_1", 0.0)],
            "openings": [],
        },
    )
    _write_json(
        root / "room_plan/room_plan_report.json",
        {
            "schema": "capture_splat.room_plan_report.v0.1",
            "room_plan_file": "room_plan/room.usdz",
            "room_semantics_file": "room_plan/room_semantics.json",
            "doors": 2,
            "openings": 0,
        },
    )
    _write_json(root / "capture.json", prepared)
    return root / "capture.json"


def _sfm_package(path: Path, capture: Path, *, registered_name: str = "frame_000001.jpg") -> Path:
    (path / "sparse/0").mkdir(parents=True)
    (path / "images").mkdir()
    source = capture.parent / "images/frame_000001.jpg"
    destination = path / "images" / registered_name
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(source.read_bytes())
    (path / "sparse/0/images.txt").write_text(
        f"1 1 0 0 0 0 0 0 1 {registered_name}\n\n",
        encoding="utf-8",
    )
    return path


def _source(capture: Path) -> tuple[Path, dict[str, object]]:
    path = capture.parent / "metadata/source_capture.json"
    return path, json.loads(path.read_text(encoding="utf-8"))


def _trajectory(capture: Path) -> tuple[Path, list[dict[str, object]]]:
    path = capture.parent / "metadata/frame_index.jsonl"
    return path, [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _write_trajectory(path: Path, values: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(json.dumps(value, allow_nan=False) + "\n" for value in values),
        encoding="utf-8",
    )


def test_derivation_identifies_crossing_but_holds_missing_rgbd(tmp_path: Path) -> None:
    capture = _fixture(tmp_path)
    report = derive_portal_route_evidence(
        capture,
        tmp_path / "out",
        sfm_package=_sfm_package(tmp_path / "sfm", capture),
    )

    assert report["decision"] == "hold"
    assert report["portal_analysis"]["selected_portal_id"] == "door_1"
    door_0 = next(value for value in report["portal_analysis"]["candidates"] if value["id"] == "door_0")
    door = next(value for value in report["portal_analysis"]["candidates"] if value["id"] == "door_1")
    assert door_0["crossing_count"] == 0
    assert door["crossing_count"] == 1
    assert report["frame_bindings"]["accepted_rgbd_region_counts"] == {
        "side_a": 0,
        "through_opening": 0,
        "side_b": 1,
    }
    assert report["frame_bindings"]["registered_accepted_rgbd_region_counts"] == {
        "side_a": 0,
        "through_opening": 0,
        "side_b": 1,
    }
    assert "accepted_rgbd_side_a_missing" in report["hold_reasons"]
    assert "accepted_rgbd_through_opening_missing" in report["hold_reasons"]
    assert report["frame_bindings"]["synthetic_rgbd_generated"] is False
    assert report["outcome"]["traversable"] is False
    assert not any(report["authority"].values())
    assert report["colmap_registration"]["registered_prepared_image_parity"]["count"] == 1


def test_derivation_without_colmap_keeps_registered_counts_zero(tmp_path: Path) -> None:
    capture = _fixture(tmp_path)
    report = derive_portal_route_evidence(capture, tmp_path / "out")

    assert report["colmap_registration"]["supplied"] is False
    assert "colmap_registration_missing" in report["hold_reasons"]
    assert report["frame_bindings"]["registered_accepted_rgbd_region_counts"] == {
        "side_a": 0,
        "through_opening": 0,
        "side_b": 0,
    }


def test_derivation_is_byte_deterministic(tmp_path: Path) -> None:
    capture = _fixture(tmp_path)
    derive_portal_route_evidence(capture, tmp_path / "out_a")
    derive_portal_route_evidence(capture, tmp_path / "out_b")

    assert (tmp_path / "out_a" / REPORT_NAME).read_bytes() == (
        tmp_path / "out_b" / REPORT_NAME
    ).read_bytes()


def test_derivation_rejects_broken_rgbd_source_binding(tmp_path: Path) -> None:
    capture = _fixture(tmp_path)
    payload = json.loads(capture.read_text(encoding="utf-8"))
    payload["frames"][2]["transform_matrix"] = _pose(-0.5)
    _write_json(capture, payload)

    with pytest.raises(ValueError, match="pose does not match"):
        derive_portal_route_evidence(capture, tmp_path / "out")
    rejected = json.loads((tmp_path / "out" / REPORT_NAME).read_text(encoding="utf-8"))
    assert rejected["decision"] == "reject"
    assert not any(rejected["authority"].values())


def test_derivation_rejects_truncated_full_trajectory(tmp_path: Path) -> None:
    capture = _fixture(tmp_path)
    source_path, source = _source(capture)
    source["video_frame_count"] = 3
    _write_json(source_path, source)

    with pytest.raises(ValueError, match="sample count does not match"):
        derive_portal_route_evidence(capture, tmp_path / "out")
    rejected = json.loads((tmp_path / "out" / REPORT_NAME).read_text(encoding="utf-8"))
    assert rejected["decision"] == "reject"
    assert not any(rejected["authority"].values())


def test_derivation_rejects_missing_source_video_frame_count(tmp_path: Path) -> None:
    capture = _fixture(tmp_path)
    source_path, source = _source(capture)
    source.pop("video_frame_count")
    _write_json(source_path, source)

    with pytest.raises(ValueError, match="video_frame_count"):
        derive_portal_route_evidence(capture, tmp_path / "out")


def test_derivation_rejects_mismatched_source_trajectory_reference(tmp_path: Path) -> None:
    capture = _fixture(tmp_path)
    source_path, source = _source(capture)
    source["frame_index_file"] = "metadata/other.jsonl"
    _write_json(source_path, source)

    with pytest.raises(ValueError, match="trajectory references do not match"):
        derive_portal_route_evidence(capture, tmp_path / "out")


def test_derivation_rejects_noncontiguous_trajectory_indices(tmp_path: Path) -> None:
    capture = _fixture(tmp_path)
    trajectory_path, trajectory = _trajectory(capture)
    trajectory[1]["video_frame_idx"] = 2
    _write_trajectory(trajectory_path, trajectory)

    with pytest.raises(ValueError, match="exactly 0..N-1"):
        derive_portal_route_evidence(capture, tmp_path / "out")


def test_derivation_holds_crossing_with_non_normal_tracking(tmp_path: Path) -> None:
    capture = _fixture(tmp_path)
    trajectory_path, trajectory = _trajectory(capture)
    trajectory[0]["tracking_state"] = "limited"
    _write_trajectory(trajectory_path, trajectory)

    report = derive_portal_route_evidence(capture, tmp_path / "out")

    door = next(value for value in report["portal_analysis"]["candidates"] if value["id"] == "door_1")
    assert report["portal_analysis"]["selected_portal_id"] is None
    assert door["crossing_count"] == 0
    assert door["rejected_crossing_count"] == 1
    assert door["rejected_crossings"][0]["reasons"] == ["tracking_not_normal"]
    assert "trajectory_portal_crossing_bracket_invalid" in report["hold_reasons"]
    assert not any(report["authority"].values())


@pytest.mark.parametrize(
    ("first_x", "second_x", "second_timestamp", "expected_reason"),
    [
        (-0.1, 0.1, 2.0, "timestamp_gap_exceeds_limit"),
        (-0.3, 0.3, 1.1, "translation_exceeds_limit"),
        (-0.1, 0.1, 1.05, "speed_exceeds_limit"),
    ],
)
def test_derivation_holds_crossing_outside_bracket_limits(
    tmp_path: Path,
    first_x: float,
    second_x: float,
    second_timestamp: float,
    expected_reason: str,
) -> None:
    capture = _fixture(tmp_path)
    trajectory_path, trajectory = _trajectory(capture)
    trajectory[0]["camera_to_world"] = _pose(first_x)
    trajectory[1]["camera_to_world"] = _pose(second_x)
    trajectory[1]["ar_timestamp"] = second_timestamp
    _write_trajectory(trajectory_path, trajectory)
    prepared = json.loads(capture.read_text(encoding="utf-8"))
    prepared["frames"][0]["transform_matrix"] = _pose(first_x)
    prepared["frames"][1]["transform_matrix"] = _pose(second_x)
    prepared["frames"][1]["timestamp"] = second_timestamp
    _write_json(capture, prepared)

    report = derive_portal_route_evidence(capture, tmp_path / "out")

    door = next(value for value in report["portal_analysis"]["candidates"] if value["id"] == "door_1")
    assert door["crossing_count"] == 0
    assert expected_reason in door["rejected_crossings"][0]["reasons"]
    assert not any(report["authority"].values())


def test_derivation_rejects_colmap_path_alias_instead_of_basename_matching(tmp_path: Path) -> None:
    capture = _fixture(tmp_path)
    sfm = _sfm_package(tmp_path / "sfm", capture)
    (sfm / "sparse/0/images.txt").write_text(
        "1 1 0 0 0 0 0 0 1 ../frame_000001.jpg\n\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="canonical relative POSIX path"):
        derive_portal_route_evidence(capture, tmp_path / "out", sfm_package=sfm)


def test_derivation_rejects_registered_image_byte_mismatch(tmp_path: Path) -> None:
    capture = _fixture(tmp_path)
    sfm = _sfm_package(tmp_path / "sfm", capture)
    (sfm / "images/frame_000001.jpg").write_bytes(b"different bytes")

    with pytest.raises(ValueError, match="bytes do not match"):
        derive_portal_route_evidence(capture, tmp_path / "out", sfm_package=sfm)


def test_derivation_rejects_registered_image_symlink(tmp_path: Path) -> None:
    capture = _fixture(tmp_path)
    sfm = _sfm_package(tmp_path / "sfm", capture)
    image = sfm / "images/frame_000001.jpg"
    target = sfm / "target.jpg"
    target.write_bytes(image.read_bytes())
    image.unlink()
    image.symlink_to(target)

    with pytest.raises(ValueError, match="escapes package|regular non-symlink file"):
        derive_portal_route_evidence(capture, tmp_path / "out", sfm_package=sfm)


def test_derivation_rejects_sfm_package_symlink(tmp_path: Path) -> None:
    capture = _fixture(tmp_path)
    real = _sfm_package(tmp_path / "real_sfm", capture)
    link = tmp_path / "sfm_link"
    link.symlink_to(real, target_is_directory=True)

    with pytest.raises(ValueError, match="regular non-symlink directory"):
        derive_portal_route_evidence(capture, tmp_path / "out", sfm_package=link)


def test_derivation_refuses_output_inside_immutable_inputs(tmp_path: Path) -> None:
    capture = _fixture(tmp_path)
    sfm = _sfm_package(tmp_path / "sfm", capture)

    with pytest.raises(ValueError, match="immutable prepared capture"):
        derive_portal_route_evidence(capture, capture.parent / "diagnostic")
    with pytest.raises(ValueError, match="immutable SfM package"):
        derive_portal_route_evidence(capture, sfm / "diagnostic", sfm_package=sfm)


def test_derivation_rejects_trajectory_changed_during_analysis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture = _fixture(tmp_path)
    trajectory_path, _ = _trajectory(capture)
    original_crossing = portal_route_derivation._crossing
    changed = False

    def mutating_crossing(
        previous: dict[str, object], current: dict[str, object], portal: dict[str, object]
    ) -> tuple[dict[str, object] | None, dict[str, object] | None]:
        nonlocal changed
        if not changed:
            trajectory_path.write_bytes(trajectory_path.read_bytes() + b" \n")
            changed = True
        return original_crossing(previous, current, portal)

    monkeypatch.setattr(portal_route_derivation, "_crossing", mutating_crossing)

    with pytest.raises(ValueError, match="changed while it was read"):
        derive_portal_route_evidence(capture, tmp_path / "out")


def test_snapshot_rejects_json_changed_during_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "input.json"
    path.write_bytes(b'{"padding":"' + b"a" * (2 * 1024 * 1024) + b'"}\n')
    original_read = portal_route_derivation.os.read
    changed = False

    def mutating_read(descriptor: int, count: int) -> bytes:
        nonlocal changed
        chunk = original_read(descriptor, count)
        if chunk and not changed:
            path.write_bytes(b'{"padding":"' + b"b" * (2 * 1024 * 1024) + b'"}\n')
            changed = True
        return chunk

    monkeypatch.setattr(portal_route_derivation.os, "read", mutating_read)

    with pytest.raises(ValueError, match="changed while it was read"):
        portal_route_derivation._snapshot(path, "JSON input", path.name, collect=True)


def test_derivation_cli_dispatches_sfm_package(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    capture = _fixture(tmp_path)
    sfm = _sfm_package(tmp_path / "sfm", capture)
    out = tmp_path / "out"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "capture-splat",
            "derive-portal-route-evidence",
            "--prepared-capture",
            str(capture),
            "--sfm-package",
            str(sfm),
            "--out",
            str(out),
        ],
    )

    cli.main()

    report = json.loads(capsys.readouterr().out)
    assert report["decision"] == "hold"
    assert report["colmap_registration"]["registered_prepared_image_count"] == 1
    assert report["colmap_registration"]["registered_prepared_image_parity"]["count"] == 1
    assert (out / REPORT_NAME).exists()
