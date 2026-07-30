from __future__ import annotations

import copy
import hashlib
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from capture_splat.json_utils import load_json_strict, write_json_strict
from capture_splat.live_session import (
    build_live_replay_plan,
    derive_live_session_id,
    validate_live_ack,
    validate_live_finalize,
    validate_live_frame,
    validate_live_session,
    validate_safe_relative_path,
)

CONTRACT_ROOT = Path(__file__).parents[1] / "contracts" / "live-session"
V0_1_ROOT = CONTRACT_ROOT / "v0.1"
V0_2_ROOT = CONTRACT_ROOT / "v0.2"
FINGERPRINTS = {
    "v0.1/schemas/capture_splat.live_ack.v0.1.schema.json": "618b686f7f8d831f3b7a66937235f6e08e2eed2f3681814fb2fabeb9ba528475",
    "v0.1/schemas/capture_splat.live_frame.v0.1.schema.json": "adf7736e46f5f0b97308ea17c0e03c1667687979ceff5e2ad03fe44c1023ed65",
    "v0.1/schemas/capture_splat.live_session.v0.1.schema.json": "cf4a52128e94b0406371f1153601d02758ef3ff10bbe471ea5bbd37a51fe3d8c",
    "v0.1/fixtures/valid_ack.json": "9831be99f01ece69ab5686fa90c64e6f397d9a27745cd749a7d98ad6c18b33c0",
    "v0.1/fixtures/valid_frame.json": "0c24c293077e52677f8ca17500cd389f31b8bf863974f8d99ccc7c1b76c32187",
    "v0.1/fixtures/valid_session.json": "98e1f2e0ca8d8796f9ed02301eacfeab19affbd6a58f52bb4fadbdf8b098f887",
    "v0.2/schemas/capture_splat.live_finalize.v0.2.schema.json": "0993b56961fa5db67435519221e42faf58be3fcf5444b356d6ac3b4cdfbcded6",
    "v0.2/schemas/capture_splat.live_session.v0.2.schema.json": "b6381ceec3bf45567956af400d698875e9da80284ce8196896f243437bb07937",
    "v0.2/fixtures/valid_finalize.json": "1a603891e4a36c873253419a19d003b80e6f1f4ea86716d9275693bafb25c76a",
    "v0.2/fixtures/valid_session.json": "efd5516efb53d64eb2806030df9baa2ad40c5d404d0ed38bd9dbbb84bb954773",
}


def _write_capture(root: Path, *, rgb: str = "rgb/frame.jpg", timestamp: float | None = 1.25) -> Path:
    (root / "rgb").mkdir(parents=True)
    (root / "depth").mkdir()
    (root / "confidence").mkdir()
    (root / "masks/person").mkdir(parents=True)
    (root / "masks/valid").mkdir()
    (root / "masks/object").mkdir()
    Image.new("RGB", (8, 6), (10, 20, 30)).save(root / "rgb/frame.jpg")
    np.save(root / "depth/frame.npy", np.ones((3, 4), dtype=np.float32), allow_pickle=False)
    np.save(root / "confidence/frame.npy", np.full((3, 4), 2, dtype=np.uint8), allow_pickle=False)
    for kind in ("person", "valid", "object"):
        Image.new("L", (4, 3), 255).save(root / f"masks/{kind}/frame.png")
    frame = {
        "rgb": rgb,
        "depth": "depth/frame.npy",
        "confidence": "confidence/frame.npy",
        "person_mask": "masks/person/frame.png",
        "valid_mask": "masks/valid/frame.png",
        "object_mask": "masks/object/frame.png",
        "transform_matrix": [[1, 0, 0, 0.1], [0, 1, 0, 1.2], [0, 0, 1, -0.3], [0, 0, 0, 1]],
        "intrinsics": {"fl_x": 4.0, "fl_y": 3.0, "cx": 2.0, "cy": 1.5, "w": 4, "h": 3},
        "tracking_state": "normal",
        "capture_quality": {
            "accepted": True,
            "reason": "useful_keyframe",
            "score": 0.9,
            "blur_score": 0.01,
            "valid_depth_ratio": 0.8,
            "feature_point_count": 42,
        },
    }
    if timestamp is not None:
        frame["timestamp"] = timestamp
    write_json_strict(root / "capture.json", {
        "schema": "capture_splat.v0.3",
        "session_config": {"scale_authority": "arkit_vio_metric", "up_axis": [0, 1, 0]},
        "intrinsics": {"fl_x": 4.0, "fl_y": 3.0, "cx": 2.0, "cy": 1.5, "w": 4, "h": 3},
        "frames": [
            frame,
            {
                "rgb": "rgb/frame.jpg",
                "timestamp": 2.0,
                "transform_matrix": [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]],
                "capture_quality": {"accepted": False, "reason": "low_blur_score"},
            },
        ],
    })
    return root


def test_contract_files_have_pinned_fingerprints_and_strict_objects() -> None:
    assert {path.as_posix(): hashlib.sha256((CONTRACT_ROOT / path).read_bytes()).hexdigest() for path in map(Path, FINGERPRINTS)} == FINGERPRINTS
    for relative in FINGERPRINTS:
        payload = load_json_strict(CONTRACT_ROOT / relative)
        if "/schemas/" in relative:
            pending = [payload]
            while pending:
                value = pending.pop()
                if isinstance(value, dict):
                    if value.get("type") == "object":
                        assert value.get("additionalProperties") is False
                    pending.extend(value.values())
                elif isinstance(value, list):
                    pending.extend(value)


def test_valid_contract_fixtures_pass_dependency_free_validation() -> None:
    validate_live_session(load_json_strict(V0_1_ROOT / "fixtures/valid_session.json"))
    validate_live_frame(load_json_strict(V0_1_ROOT / "fixtures/valid_frame.json"))
    validate_live_ack(load_json_strict(V0_1_ROOT / "fixtures/valid_ack.json"))
    validate_live_session(load_json_strict(V0_2_ROOT / "fixtures/valid_session.json"))
    validate_live_finalize(load_json_strict(V0_2_ROOT / "fixtures/valid_finalize.json"))


def test_progressive_session_seed_has_a_deterministic_derived_identity() -> None:
    seed = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"
    assert derive_live_session_id(seed) == "csl_SMOhjzjH7dE8x3yB5A0KBAo4YL6A4IzY1U570kVX_D8"


@pytest.mark.parametrize(
    "seed",
    [
        "",
        "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=",
        "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh",
        "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh9",
        "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh!",
    ],
)
def test_progressive_session_rejects_noncanonical_or_wrong_length_seed(seed: str) -> None:
    session = load_json_strict(V0_2_ROOT / "fixtures/valid_session.json")
    session["source_session_seed_b64u"] = seed
    with pytest.raises(ValueError, match="canonical unpadded Base64URL"):
        validate_live_session(session)


def test_progressive_session_rejects_wrong_identity_count_and_cross_version_fields() -> None:
    session = load_json_strict(V0_2_ROOT / "fixtures/valid_session.json")
    invalid = copy.deepcopy(session)
    invalid["session_id"] = "csl_" + "A" * 43
    with pytest.raises(ValueError, match="does not match"):
        validate_live_session(invalid)

    invalid = copy.deepcopy(session)
    invalid["expected_frame_count"] = 2
    with pytest.raises(ValueError, match="must be null"):
        validate_live_session(invalid)

    invalid = copy.deepcopy(session)
    invalid["source_manifest"] = {
        "path": "capture.json",
        "sha256": "sha256:" + "0" * 64,
        "size_bytes": 1,
        "schema": "capture_splat.v0.3",
    }
    with pytest.raises(ValueError, match="unexpected keys"):
        validate_live_session(invalid)

    replay_session = load_json_strict(V0_1_ROOT / "fixtures/valid_session.json")
    replay_session["source_session_seed_b64u"] = session["source_session_seed_b64u"]
    with pytest.raises(ValueError, match="unexpected keys"):
        validate_live_session(replay_session)


def test_progressive_finalize_rejects_corrupt_or_conflicting_evidence() -> None:
    finalize = load_json_strict(V0_2_ROOT / "fixtures/valid_finalize.json")
    mutations = [
        ("session_id", "fixture-session-01"),
        ("session_id", "csl_" + "A" * 42 + "9"),
        ("final_sequence_id", 0),
        ("final_sequence_id", 100_000_000),
        ("final_sequence_id", True),
        ("source_manifest.path", "../capture.json"),
        ("source_manifest.sha256", "sha256:" + "A" * 64),
        ("source_manifest.size_bytes", 0),
        ("source_manifest.size_bytes", float("nan")),
        ("source_manifest.schema", ""),
        ("unexpected", True),
    ]
    for field, value in mutations:
        invalid = copy.deepcopy(finalize)
        if field.startswith("source_manifest."):
            invalid["source_manifest"][field.split(".", 1)[1]] = value
        else:
            invalid[field] = value
        with pytest.raises(ValueError):
            validate_live_finalize(invalid)


def test_finalize_versions_are_strict_and_do_not_mix() -> None:
    replay_finalize = {
        "schema": "capture_splat.live_finalize.v0.1",
        "session_id": "fixture-session-01",
        "final_sequence_id": 2,
    }
    assert validate_live_finalize(replay_finalize) == replay_finalize

    progressive_finalize = load_json_strict(V0_2_ROOT / "fixtures/valid_finalize.json")
    mixed_replay = copy.deepcopy(progressive_finalize)
    mixed_replay["schema"] = "capture_splat.live_finalize.v0.1"
    with pytest.raises(ValueError, match="unexpected keys"):
        validate_live_finalize(mixed_replay)

    mixed_progressive = copy.deepcopy(replay_finalize)
    mixed_progressive["schema"] = "capture_splat.live_finalize.v0.2"
    with pytest.raises(ValueError, match="missing keys"):
        validate_live_finalize(mixed_progressive)


@pytest.mark.parametrize("path", ["/tmp/frame.jpg", "../frame.jpg", "rgb/../frame.jpg", "rgb//frame.jpg", "./rgb/frame.jpg", "file:///tmp/a", "https://host/a", r"rgb\frame.jpg", "rgb/\x00frame.jpg"])
def test_paths_reject_absolute_uri_backslash_and_traversal(path: str) -> None:
    with pytest.raises(ValueError, match="safe POSIX-relative"):
        validate_safe_relative_path(path)


def test_contract_rejects_non_finite_bad_checksum_and_additional_properties() -> None:
    frame = load_json_strict(V0_1_ROOT / "fixtures/valid_frame.json")
    invalid = copy.deepcopy(frame)
    invalid["camera_to_world"][0] = float("nan")
    with pytest.raises(ValueError, match="non-finite"):
        validate_live_frame(invalid)
    invalid = copy.deepcopy(frame)
    invalid["source_frame"]["sha256"] = "sha256:ABC"
    with pytest.raises(ValueError, match="64 lowercase hex"):
        validate_live_frame(invalid)
    invalid = copy.deepcopy(frame)
    invalid["unexpected"] = True
    with pytest.raises(ValueError, match="unexpected keys"):
        validate_live_frame(invalid)
    invalid = copy.deepcopy(frame)
    invalid["source_frame"]["media_type"] = "Image/JPEG"
    with pytest.raises(ValueError, match="lowercase MIME"):
        validate_live_frame(invalid)
    invalid = copy.deepcopy(frame)
    invalid["assets"] = {}
    with pytest.raises(ValueError, match="must not be empty"):
        validate_live_frame(invalid)
    session = load_json_strict(V0_1_ROOT / "fixtures/valid_session.json")
    session["created_at"] = "2026-01-02 03:04:05+00:00"
    with pytest.raises(ValueError, match="RFC 3339"):
        validate_live_session(session)


def test_build_plan_preserves_calibration_dimensions_and_all_sidecars(tmp_path: Path) -> None:
    capture = _write_capture(tmp_path / "capture")
    plan = build_live_replay_plan(capture, "test-session")

    assert plan.session["expected_frame_count"] == 1
    assert plan.session["coordinate_system"]["id"] == "arkit_world"
    assert plan.session["coordinate_system"]["units"] == "meters"
    assert plan.session["authority"] == "proposal_only"
    frame = plan.frames[0]
    assert frame.sequence_id == 1
    assert frame.metadata["source_frame"]["width"] == 8
    assert frame.metadata["source_frame"]["height"] == 6
    assert frame.metadata["intrinsics"]["calibration_width"] == 4
    assert frame.metadata["intrinsics"]["calibration_height"] == 3
    assert frame.metadata["intrinsics"]["applies_to"] == "depth"
    assert frame.metadata["camera_to_world"][3:12:4] == [0.1, 1.2, -0.3]
    assert [asset.role for asset in frame.assets] == [
        "source", "depth", "confidence", "mask-person", "mask-valid", "mask-object"
    ]
    assert [mask["kind"] for mask in frame.metadata["assets"]["masks"]] == ["person", "valid", "object"]


def test_build_plan_keeps_optional_image_sidecar_when_dimensions_are_not_decodable(tmp_path: Path) -> None:
    capture = _write_capture(tmp_path / "capture")
    manifest = load_json_strict(capture / "capture.json")
    manifest["frames"][0]["person_mask"] = "masks/person/frame.svg"
    (capture / "masks/person/frame.svg").write_text("<svg xmlns=\"http://www.w3.org/2000/svg\"/>")
    write_json_strict(capture / "capture.json", manifest)

    frame = build_live_replay_plan(capture, "test-session").frames[0]
    person = frame.metadata["assets"]["masks"][0]
    assert person["media_type"] == "image/svg+xml"
    assert "width" not in person
    assert "height" not in person


def test_build_plan_rejects_traversal_and_symlink_escape(tmp_path: Path) -> None:
    capture = _write_capture(tmp_path / "capture", rgb="../outside.jpg")
    (tmp_path / "outside.jpg").write_bytes(b"outside")
    with pytest.raises(ValueError, match="safe POSIX-relative"):
        build_live_replay_plan(capture, "test-session")

    capture = _write_capture(tmp_path / "capture-symlink", rgb="rgb/link.jpg")
    outside = tmp_path / "outside-real.jpg"
    Image.new("RGB", (2, 2)).save(outside)
    (capture / "rgb/link.jpg").symlink_to(outside)
    with pytest.raises(ValueError, match="escapes the capture root"):
        build_live_replay_plan(capture, "test-session")


def test_build_plan_requires_timestamp(tmp_path: Path) -> None:
    capture = _write_capture(tmp_path / "capture", timestamp=None)
    with pytest.raises(ValueError, match="missing timestamp"):
        build_live_replay_plan(capture, "test-session")


def test_build_plan_rejects_manifest_symlink_escape(tmp_path: Path) -> None:
    capture = _write_capture(tmp_path / "capture")
    outside = tmp_path / "outside-capture.json"
    (capture / "capture.json").replace(outside)
    (capture / "capture.json").symlink_to(outside)
    with pytest.raises(ValueError, match="capture.json escapes"):
        build_live_replay_plan(capture, "test-session")
