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
    parity = report["colmap_registration"]["registered_prepared_image_parity"]
    expected_image_bytes = (capture.parent / "images/frame_000001.jpg").stat().st_size
    assert parity["count"] == 1
    assert parity["sfm_bytes_hashed"] == expected_image_bytes
    assert parity["prepared_bytes_hashed"] == expected_image_bytes
    assert parity["combined_bytes_hashed"] == expected_image_bytes * 2
    assert parity["comparison_order"] == "canonical_path_then_size_then_sha256"
    assert (tmp_path / "out" / REPORT_NAME).stat().st_mode & 0o777 == 0o600


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


def test_requested_portal_does_not_bypass_unique_observed_crossing(tmp_path: Path) -> None:
    capture = _fixture(tmp_path)

    report = derive_portal_route_evidence(
        capture, tmp_path / "out", portal_id="door_0"
    )

    assert report["portal_analysis"]["requested_portal_id"] == "door_0"
    assert report["portal_analysis"]["selected_portal_id"] is None
    assert report["portal_analysis"]["selection"] == "requested_crossing_mismatch"
    assert report["rails"]["trajectory_portal_crossing"] == "held_missing"


def test_requested_portal_selects_only_after_unique_observed_crossing(tmp_path: Path) -> None:
    capture = _fixture(tmp_path)

    report = derive_portal_route_evidence(
        capture, tmp_path / "out", portal_id="door_1"
    )

    assert report["portal_analysis"]["selected_portal_id"] == "door_1"
    assert report["portal_analysis"]["selection"] == "requested_unique_observed_crossing"


def test_requested_portal_holds_when_multiple_portals_cross(tmp_path: Path) -> None:
    capture = _fixture(tmp_path)
    semantics_path = capture.parent / "room_plan/room_semantics.json"
    semantics = json.loads(semantics_path.read_text(encoding="utf-8"))
    semantics["doors"][0] = _door("door_0", 0.0)
    _write_json(semantics_path, semantics)

    report = derive_portal_route_evidence(
        capture, tmp_path / "out", portal_id="door_1"
    )

    assert report["portal_analysis"]["selected_portal_id"] is None
    assert report["portal_analysis"]["selection"] == "requested_crossing_ambiguous"


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
    image = sfm / "images/frame_000001.jpg"
    original = image.read_bytes()
    image.write_bytes(bytes([original[0] ^ 1]) + original[1:])

    with pytest.raises(ValueError, match="bytes do not match"):
        derive_portal_route_evidence(capture, tmp_path / "out", sfm_package=sfm)


def test_derivation_compares_registered_image_size_before_hashing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture = _fixture(tmp_path)
    sfm = _sfm_package(tmp_path / "sfm", capture)
    (sfm / "images/frame_000001.jpg").write_bytes(b"different size")
    original_snapshot = portal_route_derivation._ConfinedRoot.snapshot
    parity_hashes: list[str] = []

    def tracking_snapshot(
        self: object,
        relative: object,
        label: str,
        **kwargs: object,
    ) -> tuple[bytes | None, dict[str, object]]:
        if label.startswith(("registered SfM image", "prepared image matching")):
            parity_hashes.append(label)
        return original_snapshot(self, relative, label, **kwargs)

    monkeypatch.setattr(
        portal_route_derivation._ConfinedRoot, "snapshot", tracking_snapshot
    )

    with pytest.raises(ValueError, match="size does not match"):
        derive_portal_route_evidence(capture, tmp_path / "out", sfm_package=sfm)
    assert parity_hashes == []


@pytest.mark.parametrize(
    "pose_record",
    [
        "0 1 0 0 0 0 0 0 1 frame_000001.jpg",
        "-1 1 0 0 0 0 0 0 1 frame_000001.jpg",
        "1 1 0 0 0 0 0 0 0 frame_000001.jpg",
        "1 1 0 0 0 0 0 0 -1 frame_000001.jpg",
        "1 2 0 0 0 0 0 0 1 frame_000001.jpg",
    ],
)
def test_derivation_rejects_invalid_colmap_ids_and_quaternion(
    tmp_path: Path, pose_record: str
) -> None:
    capture = _fixture(tmp_path)
    sfm = _sfm_package(tmp_path / "sfm", capture)
    (sfm / "sparse/0/images.txt").write_text(f"{pose_record}\n\n", encoding="utf-8")

    with pytest.raises(ValueError, match="registration records are invalid"):
        derive_portal_route_evidence(capture, tmp_path / "out", sfm_package=sfm)


def test_derivation_rejects_duplicate_positive_colmap_image_ids(tmp_path: Path) -> None:
    capture = _fixture(tmp_path)
    sfm = _sfm_package(tmp_path / "sfm", capture)
    (sfm / "images/other.jpg").write_bytes(b"other")
    (sfm / "sparse/0/images.txt").write_text(
        "1 1 0 0 0 0 0 0 1 frame_000001.jpg\n\n"
        "1 1 0 0 0 0 0 0 1 other.jpg\n\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="registration records are invalid"):
        derive_portal_route_evidence(capture, tmp_path / "out", sfm_package=sfm)


def test_derivation_rejects_colmap_casefold_path_aliases(tmp_path: Path) -> None:
    capture = _fixture(tmp_path)
    sfm = _sfm_package(tmp_path / "sfm", capture)
    (sfm / "images/FRAME_000001.JPG").write_bytes(b"other")
    (sfm / "sparse/0/images.txt").write_text(
        "1 1 0 0 0 0 0 0 1 frame_000001.jpg\n\n"
        "2 1 0 0 0 0 0 0 1 FRAME_000001.JPG\n\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="casefold aliases"):
        derive_portal_route_evidence(capture, tmp_path / "out", sfm_package=sfm)


def test_derivation_rejects_registered_image_inode_aliases(tmp_path: Path) -> None:
    capture = _fixture(tmp_path)
    sfm = _sfm_package(tmp_path / "sfm", capture)
    (sfm / "images/frame_alias.jpg").hardlink_to(sfm / "images/frame_000001.jpg")
    (sfm / "sparse/0/images.txt").write_text(
        "1 1 0 0 0 0 0 0 1 frame_000001.jpg\n\n"
        "2 1 0 0 0 0 0 0 1 frame_alias.jpg\n\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="inode alias"):
        derive_portal_route_evidence(capture, tmp_path / "out", sfm_package=sfm)


def test_derivation_rejects_registered_image_inode_alias_of_prepared_rgb(
    tmp_path: Path,
) -> None:
    capture = _fixture(tmp_path)
    sfm = _sfm_package(tmp_path / "sfm", capture)
    image = sfm / "images/frame_000001.jpg"
    image.unlink()
    image.hardlink_to(capture.parent / "images/frame_000001.jpg")

    with pytest.raises(ValueError, match="inode alias of prepared RGB"):
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


def test_derivation_rejects_physical_component_case_mismatch(tmp_path: Path) -> None:
    capture = _fixture(tmp_path)
    (capture.parent / "images").rename(capture.parent / "Images")

    with pytest.raises(ValueError, match="physical path component casing"):
        derive_portal_route_evidence(capture, tmp_path / "out")


def test_derivation_rejects_physical_casefold_leaf_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture = _fixture(tmp_path)
    original_directory_names = portal_route_derivation._directory_names

    def aliased_directory_names(
        descriptor: int, label: str, **kwargs: object
    ) -> list[str]:
        names = original_directory_names(descriptor, label, **kwargs)
        if "frame_000001.jpg" in names:
            return [*names, "FRAME_000001.JPG"]
        return names

    monkeypatch.setattr(
        portal_route_derivation, "_directory_names", aliased_directory_names
    )

    with pytest.raises(ValueError, match="casefold path alias"):
        derive_portal_route_evidence(capture, tmp_path / "out")


def test_derivation_detects_confined_parent_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture = _fixture(tmp_path)
    room_plan = capture.parent / "room_plan"
    semantics_inode = (room_plan / "room_semantics.json").stat().st_ino
    moved = capture.parent / "room_plan_original"
    attacker = tmp_path / "attacker_room_plan"
    attacker.mkdir()
    original_read = portal_route_derivation.os.read
    swapped = False

    def swapping_read(descriptor: int, count: int) -> bytes:
        nonlocal swapped
        chunk = original_read(descriptor, count)
        if (
            chunk
            and not swapped
            and portal_route_derivation.os.fstat(descriptor).st_ino
            == semantics_inode
        ):
            room_plan.rename(moved)
            room_plan.symlink_to(attacker, target_is_directory=True)
            swapped = True
        return chunk

    monkeypatch.setattr(portal_route_derivation.os, "read", swapping_read)

    with pytest.raises(ValueError, match="directory path changed"):
        derive_portal_route_evidence(capture, tmp_path / "out")
    rejected = json.loads((tmp_path / "out" / REPORT_NAME).read_text(encoding="utf-8"))
    assert rejected["decision"] == "reject"
    assert not (attacker / REPORT_NAME).exists()


def test_derivation_detects_confined_leaf_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture = _fixture(tmp_path)
    semantics = capture.parent / "room_plan/room_semantics.json"
    semantics_inode = semantics.stat().st_ino
    original = semantics.with_name("room_semantics_original.json")
    attacker = tmp_path / "attacker_semantics.json"
    attacker.write_text("{}\n", encoding="utf-8")
    original_read = portal_route_derivation.os.read
    swapped = False

    def swapping_read(descriptor: int, count: int) -> bytes:
        nonlocal swapped
        chunk = original_read(descriptor, count)
        if (
            chunk
            and not swapped
            and portal_route_derivation.os.fstat(descriptor).st_ino
            == semantics_inode
        ):
            semantics.rename(original)
            semantics.symlink_to(attacker)
            swapped = True
        return chunk

    monkeypatch.setattr(portal_route_derivation.os, "read", swapping_read)

    with pytest.raises(ValueError, match="changed while it was read or consumed"):
        derive_portal_route_evidence(capture, tmp_path / "out")
    rejected = json.loads((tmp_path / "out" / REPORT_NAME).read_text(encoding="utf-8"))
    assert rejected["decision"] == "reject"
    assert attacker.read_text(encoding="utf-8") == "{}\n"


def test_derivation_detects_output_parent_swap_without_writing_into_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture = _fixture(tmp_path)
    output_parent = tmp_path / "output_parent"
    output_parent.mkdir()
    out = output_parent / "out"
    moved_parent = tmp_path / "moved_output_parent"
    original_derive = portal_route_derivation._derive

    def swapping_derive(*args: object, **kwargs: object) -> dict[str, object]:
        report = original_derive(*args, **kwargs)
        output_parent.rename(moved_parent)
        output_parent.symlink_to(capture.parent, target_is_directory=True)
        return report

    monkeypatch.setattr(portal_route_derivation, "_derive", swapping_derive)

    with pytest.raises(
        ValueError, match="output parent component|output parent path changed"
    ):
        derive_portal_route_evidence(capture, out)
    assert not (capture.parent / "out" / REPORT_NAME).exists()
    assert not (moved_parent / "out" / REPORT_NAME).exists()


def test_derivation_detects_output_directory_swap_without_writing_into_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture = _fixture(tmp_path)
    out = tmp_path / "out"
    moved = tmp_path / "moved_output"
    original_derive = portal_route_derivation._derive

    def swapping_derive(*args: object, **kwargs: object) -> dict[str, object]:
        report = original_derive(*args, **kwargs)
        out.rename(moved)
        out.symlink_to(capture.parent, target_is_directory=True)
        return report

    monkeypatch.setattr(portal_route_derivation, "_derive", swapping_derive)

    with pytest.raises(ValueError, match="output path changed"):
        derive_portal_route_evidence(capture, out)
    assert not (capture.parent / REPORT_NAME).exists()
    assert not (moved / REPORT_NAME).exists()


def test_derivation_detects_output_leaf_swap_without_following_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture = _fixture(tmp_path)
    capture_before = capture.read_bytes()
    out = tmp_path / "out"
    original_derive = portal_route_derivation._derive

    def swapping_derive(*args: object, **kwargs: object) -> dict[str, object]:
        report = original_derive(*args, **kwargs)
        report_path = out / REPORT_NAME
        report_path.unlink()
        report_path.symlink_to(capture)
        return report

    monkeypatch.setattr(portal_route_derivation, "_derive", swapping_derive)

    with pytest.raises(ValueError, match="report path changed|regular non-symlink"):
        derive_portal_route_evidence(capture, out)
    assert capture.read_bytes() == capture_before


def test_derivation_rejects_in_place_output_corruption_during_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture = _fixture(tmp_path)
    capture_before = capture.read_bytes()
    out = tmp_path / "out"
    report_path = out / REPORT_NAME
    original_fsync = portal_route_derivation.os.fsync
    corrupted = False

    def corrupting_fsync(descriptor: int) -> None:
        nonlocal corrupted
        original_fsync(descriptor)
        size = portal_route_derivation.os.fstat(descriptor).st_size
        if size and not corrupted:
            with report_path.open("r+b", buffering=0) as handle:
                handle.write(b"X" * size)
            corrupted = True

    monkeypatch.setattr(portal_route_derivation.os, "fsync", corrupting_fsync)

    with pytest.raises(ValueError, match="read-back bytes do not match"):
        derive_portal_route_evidence(capture, out)
    assert corrupted is True
    assert not out.exists()
    assert capture.read_bytes() == capture_before


def test_derivation_revalidates_input_after_report_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture = _fixture(tmp_path)
    semantics = capture.parent / "room_plan/room_semantics.json"
    original_write = portal_route_derivation._PinnedOutput.write
    changed = False

    def mutating_write(self: object, payload: dict[str, object]) -> None:
        nonlocal changed
        original_write(self, payload)
        if payload.get("decision") == "hold" and not changed:
            semantics.write_bytes(semantics.read_bytes() + b" ")
            changed = True

    monkeypatch.setattr(portal_route_derivation._PinnedOutput, "write", mutating_write)

    with pytest.raises(ValueError, match="input validation state changed"):
        derive_portal_route_evidence(capture, tmp_path / "out")
    assert changed is True
    assert not (tmp_path / "out").exists()


def test_derivation_revalidates_output_leaf_after_report_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture = _fixture(tmp_path)
    attacker = tmp_path / "attacker.json"
    attacker.write_bytes(b"attacker\n")
    out = tmp_path / "out"
    original_write = portal_route_derivation._PinnedOutput.write

    def swapping_write(self: object, payload: dict[str, object]) -> None:
        original_write(self, payload)
        report_path = out / REPORT_NAME
        report_path.unlink()
        report_path.symlink_to(attacker)

    monkeypatch.setattr(portal_route_derivation._PinnedOutput, "write", swapping_write)

    with pytest.raises(
        ValueError,
        match="output contents changed|report path changed|regular non-symlink",
    ):
        derive_portal_route_evidence(capture, out)
    assert attacker.read_bytes() == b"attacker\n"


def test_derivation_revalidates_rejected_report_after_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture = _fixture(tmp_path)
    prepared = json.loads(capture.read_text(encoding="utf-8"))
    prepared["frames"][2]["transform_matrix"] = _pose(-0.5)
    _write_json(capture, prepared)
    attacker = tmp_path / "attacker.json"
    attacker.write_bytes(b"attacker\n")
    out = tmp_path / "out"
    original_write = portal_route_derivation._PinnedOutput.write

    def swapping_write(self: object, payload: dict[str, object]) -> None:
        original_write(self, payload)
        report_path = out / REPORT_NAME
        report_path.unlink()
        report_path.symlink_to(attacker)

    monkeypatch.setattr(portal_route_derivation._PinnedOutput, "write", swapping_write)

    with pytest.raises(
        ValueError,
        match="output contents changed|report path changed|regular non-symlink",
    ):
        derive_portal_route_evidence(capture, out)
    assert attacker.read_bytes() == b"attacker\n"


def test_derivation_preserves_rejected_report_near_aggregate_scan_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture = _fixture(tmp_path)
    root = capture.parent
    prepared = json.loads(capture.read_text(encoding="utf-8"))
    source_path, source = _source(capture)
    source_frames: list[dict[str, object]] = []
    rgbd_frames: list[dict[str, object]] = []
    last_confidence: Path | None = None
    for index in range(40):
        timestamp = 3.0 + index
        rgb = f"images/rgb_{index:03d}/frame.jpg"
        depth = f"depth/depth_{index:03d}/frame.npy"
        confidence = f"confidence/confidence_{index:03d}/frame.npy"
        for relative in (rgb, depth, confidence):
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(relative.encode("utf-8"))
        last_confidence = root / confidence
        source_frames.append(
            {
                "timestamp": timestamp,
                "transform_matrix": _pose(0.5),
                "intrinsics": _intrinsics(),
                "capture_quality": {"accepted": True},
            }
        )
        rgbd_frames.append(
            {
                "accepted": True,
                "source_kind": "accepted_rgbd",
                "source_frame_index": index + 1,
                "timestamp": timestamp,
                "transform_matrix": _pose(0.5),
                "intrinsics": _intrinsics(),
                "rgb": rgb,
                "depth": depth,
                "confidence": confidence,
            }
        )
    source["frames"] = source_frames
    prepared["frames"] = prepared["frames"][:2] + rgbd_frames
    _write_json(source_path, source)
    _write_json(capture, prepared)

    assert last_confidence is not None
    original_derive = portal_route_derivation._derive

    def mutating_derive(*args: object, **kwargs: object) -> dict[str, object]:
        report = original_derive(*args, **kwargs)
        last_confidence.write_bytes(last_confidence.read_bytes() + b" ")
        return report

    monkeypatch.setattr(portal_route_derivation, "_derive", mutating_derive)
    out = tmp_path / "out"

    with pytest.raises(ValueError, match="changed while it was read or consumed"):
        derive_portal_route_evidence(capture, out)

    report_path = out / REPORT_NAME
    report = json.loads(report_path.read_text(encoding="utf-8"))
    expected = (json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n").encode(
        "utf-8"
    )
    assert report_path.read_bytes() == expected
    assert report["decision"] == "reject"
    assert report["error_type"] == "ValueError"
    assert "changed while it was read or consumed" in report["error"]


def test_derivation_preserves_existing_output_bytes(tmp_path: Path) -> None:
    capture = _fixture(tmp_path)
    report = tmp_path / "out" / REPORT_NAME
    report.parent.mkdir()
    report.write_bytes(b"preserve")

    with pytest.raises(FileExistsError, match="not empty"):
        derive_portal_route_evidence(capture, report.parent)
    assert report.read_bytes() == b"preserve"


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


def test_derivation_rejects_capture_json_over_byte_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture = _fixture(tmp_path)
    monkeypatch.setattr(portal_route_derivation, "_MAX_CAPTURE_JSON_BYTES", 32)

    with pytest.raises(ValueError, match="bounded regular"):
        derive_portal_route_evidence(capture, tmp_path / "out")


def test_derivation_rejects_colmap_images_over_byte_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture = _fixture(tmp_path)
    sfm = _sfm_package(tmp_path / "sfm", capture)
    images_txt = sfm / "sparse/0/images.txt"
    monkeypatch.setattr(
        portal_route_derivation, "_MAX_COLMAP_IMAGES_BYTES", images_txt.stat().st_size - 1
    )

    with pytest.raises(ValueError, match="bounded regular"):
        derive_portal_route_evidence(capture, tmp_path / "out", sfm_package=sfm)


def test_directory_enumeration_rejects_entry_count_over_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "directory"
    directory.mkdir()
    (directory / "one").touch()
    (directory / "two").touch()
    descriptor = portal_route_derivation.os.open(
        directory, portal_route_derivation.os.O_RDONLY
    )
    monkeypatch.setattr(portal_route_derivation, "_MAX_DIRECTORY_ENTRIES", 1)
    try:
        with pytest.raises(ValueError, match="bounded enumeration limit"):
            portal_route_derivation._directory_names(descriptor, "test")
    finally:
        portal_route_derivation.os.close(descriptor)


def test_directory_enumeration_rejects_name_bytes_over_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "directory"
    directory.mkdir()
    (directory / "long-name").touch()
    descriptor = portal_route_derivation.os.open(
        directory, portal_route_derivation.os.O_RDONLY
    )
    monkeypatch.setattr(portal_route_derivation, "_MAX_DIRECTORY_NAME_BYTES", 1)
    try:
        with pytest.raises(ValueError, match="bounded enumeration limit"):
            portal_route_derivation._directory_names(descriptor, "test")
    finally:
        portal_route_derivation.os.close(descriptor)


def test_directory_scan_budget_rejects_aggregate_entry_exhaustion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "directory"
    directory.mkdir()
    (directory / "one").touch()
    descriptor = portal_route_derivation.os.open(
        directory, portal_route_derivation.os.O_RDONLY
    )
    monkeypatch.setattr(
        portal_route_derivation, "_MAX_SCANNED_DIRECTORY_ENTRIES_PER_ROOT", 1
    )
    budget = portal_route_derivation._DirectoryScanBudget()
    try:
        assert budget.names(descriptor, "test") == ["one"]
        with pytest.raises(ValueError, match="aggregate directory entry limit"):
            budget.names(descriptor, "test")
        assert budget.entries == 1
    finally:
        portal_route_derivation.os.close(descriptor)


def test_directory_scan_budget_stops_at_first_entry_past_aggregate_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Entry:
        name = "extra"

    class Entries:
        yielded = 0

        def __enter__(self) -> Entries:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def __iter__(self) -> object:
            self.yielded += 1
            yield Entry()
            raise AssertionError("directory scan continued past the aggregate cap")

    entries = Entries()
    monkeypatch.setattr(portal_route_derivation.os, "scandir", lambda _: entries)
    monkeypatch.setattr(
        portal_route_derivation, "_MAX_SCANNED_DIRECTORY_ENTRIES_PER_ROOT", 1
    )
    budget = portal_route_derivation._DirectoryScanBudget(entries=1)

    with pytest.raises(ValueError, match="aggregate directory entry limit"):
        budget.names(-1, "test")
    assert entries.yielded == 1


def test_directory_scan_budget_rejects_aggregate_name_byte_exhaustion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "directory"
    directory.mkdir()
    (directory / "one").touch()
    descriptor = portal_route_derivation.os.open(
        directory, portal_route_derivation.os.O_RDONLY
    )
    monkeypatch.setattr(
        portal_route_derivation, "_MAX_SCANNED_DIRECTORY_NAME_BYTES_PER_ROOT", 3
    )
    budget = portal_route_derivation._DirectoryScanBudget()
    try:
        assert budget.names(descriptor, "test") == ["one"]
        with pytest.raises(ValueError, match="aggregate directory name byte limit"):
            budget.names(descriptor, "test")
        assert budget.name_bytes == 3
    finally:
        portal_route_derivation.os.close(descriptor)


def test_directory_scan_budget_rejects_scan_count_exhaustion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "directory"
    directory.mkdir()
    descriptor = portal_route_derivation.os.open(
        directory, portal_route_derivation.os.O_RDONLY
    )
    monkeypatch.setattr(portal_route_derivation, "_MAX_DIRECTORY_SCANS_PER_ROOT", 1)
    budget = portal_route_derivation._DirectoryScanBudget()
    try:
        assert budget.names(descriptor, "test") == []

        def unexpected_scan(_: object) -> object:
            raise AssertionError("directory scan started after scan-cap exhaustion")

        monkeypatch.setattr(portal_route_derivation.os, "scandir", unexpected_scan)
        with pytest.raises(ValueError, match="aggregate directory scan limit"):
            budget.names(descriptor, "test")
        assert budget.scans == 1
    finally:
        portal_route_derivation.os.close(descriptor)


def test_confined_root_rejects_open_directory_exhaustion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root_path = tmp_path / "root"
    (root_path / "one/two").mkdir(parents=True)
    monkeypatch.setattr(portal_route_derivation, "_MAX_OPEN_DIRECTORIES_PER_ROOT", 2)

    with portal_route_derivation._ConfinedRoot(root_path, "test") as root:
        with pytest.raises(ValueError, match="open directory limit"):
            root._directory(("one", "two"), "test")
        assert len(root._directories) == 2


def test_absolute_path_rejects_component_exhaustion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(portal_route_derivation, "_MAX_PATH_COMPONENTS", 1)

    with pytest.raises(ValueError, match="path exceeds the component limit"):
        portal_route_derivation._absolute_parts(Path("/one/two"))


def test_absolute_directory_open_tolerates_content_metadata_churn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "root"
    directory.mkdir()
    original_stat = portal_route_derivation.os.stat
    target_calls = 0

    def churning_stat(
        path: object, *args: object, **kwargs: object
    ) -> portal_route_derivation.os.stat_result:
        nonlocal target_calls
        result = original_stat(path, *args, **kwargs)
        if path != directory.name or kwargs.get("dir_fd") is None:
            return result
        target_calls += 1
        if target_calls != 2:
            return result
        values = list(result)
        values[6] += 1
        values[8] += 1
        values[9] += 1
        return portal_route_derivation.os.stat_result(values)

    monkeypatch.setattr(portal_route_derivation.os, "stat", churning_stat)
    descriptor, opened, _ = portal_route_derivation._open_absolute_directory(
        directory, "test"
    )
    try:
        assert opened == directory
        assert target_calls == 2
    finally:
        portal_route_derivation.os.close(descriptor)


def test_derivation_rejects_registered_image_over_per_image_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture = _fixture(tmp_path)
    sfm = _sfm_package(tmp_path / "sfm", capture)
    image_size = (sfm / "images/frame_000001.jpg").stat().st_size
    monkeypatch.setattr(
        portal_route_derivation, "_MAX_PARITY_IMAGE_BYTES", image_size - 1
    )

    with pytest.raises(ValueError, match="bounded regular"):
        derive_portal_route_evidence(capture, tmp_path / "out", sfm_package=sfm)


def test_derivation_rejects_registered_images_over_aggregate_bound_before_hashing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture = _fixture(tmp_path)
    sfm = _sfm_package(tmp_path / "sfm", capture)
    image_size = (sfm / "images/frame_000001.jpg").stat().st_size
    monkeypatch.setattr(
        portal_route_derivation,
        "_MAX_PARITY_COMBINED_BYTES",
        image_size * 2 - 1,
    )
    original_snapshot = portal_route_derivation._ConfinedRoot.snapshot
    parity_hashes: list[str] = []

    def tracking_snapshot(
        self: object,
        relative: object,
        label: str,
        **kwargs: object,
    ) -> tuple[bytes | None, dict[str, object]]:
        if label.startswith(("registered SfM image", "prepared image matching")):
            parity_hashes.append(label)
        return original_snapshot(self, relative, label, **kwargs)

    monkeypatch.setattr(
        portal_route_derivation._ConfinedRoot, "snapshot", tracking_snapshot
    )

    with pytest.raises(ValueError, match="aggregate byte limit"):
        derive_portal_route_evidence(capture, tmp_path / "out", sfm_package=sfm)
    assert parity_hashes == []


def test_derivation_rejects_colmap_record_count_over_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture = _fixture(tmp_path)
    sfm = _sfm_package(tmp_path / "sfm", capture)
    monkeypatch.setattr(portal_route_derivation, "_MAX_COLMAP_IMAGE_RECORDS", 0)

    with pytest.raises(ValueError, match="bounded image record"):
        derive_portal_route_evidence(capture, tmp_path / "out", sfm_package=sfm)


def test_derivation_caps_retained_crossings_but_preserves_totals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture = _fixture(tmp_path)
    source_path, source = _source(capture)
    source["video_frame_count"] = 8
    _write_json(source_path, source)
    trajectory_path, _ = _trajectory(capture)
    trajectory = [
        {
            "video_frame_idx": index,
            "ar_timestamp": 1.0 + index * 0.1,
            "camera_to_world": _pose(-0.1 if index % 2 == 0 else 0.1),
            "intrinsics": _intrinsics(),
            "tracking_state": "normal",
        }
        for index in range(8)
    ]
    _write_trajectory(trajectory_path, trajectory)
    monkeypatch.setattr(portal_route_derivation, "_MAX_RETAINED_CROSSING_EVENTS", 2)

    report = derive_portal_route_evidence(capture, tmp_path / "out")

    retention = report["trajectory"]["crossing_event_retention"]
    door = next(
        value for value in report["portal_analysis"]["candidates"] if value["id"] == "door_1"
    )
    assert retention == {
        "accepted_total": 7,
        "rejected_total": 0,
        "retained_total": 2,
        "omitted_total": 5,
        "maximum_retained": 2,
    }
    assert door["crossing_count"] == 7
    assert door["retained_crossing_count"] == 2
    assert len(door["crossings"]) == 2


def test_derivation_rejects_report_over_byte_bound_and_cleans_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture = _fixture(tmp_path)
    out = tmp_path / "out"
    monkeypatch.setattr(portal_route_derivation, "_MAX_REPORT_BYTES", 32)

    with pytest.raises(ValueError, match="report exceeds the bounded byte limit"):
        derive_portal_route_evidence(capture, out)
    assert not out.exists()


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
