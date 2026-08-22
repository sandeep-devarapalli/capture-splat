from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from capture_splat import portal_route_evidence
from capture_splat.hybrid_surface import _digest_json, _false_authority
from capture_splat.json_utils import load_json_strict, write_json_strict
from capture_splat.portal_route_evidence import (
    EVIDENCE_SCHEMA,
    FREE_SPACE_SCHEMA,
    REPORT_NAME,
    RGBD_SUPPORT_SCHEMA,
    ROOMPLAN_REGISTRATION_SCHEMA,
    ROUTE_SCHEMA,
    validate_portal_route_evidence,
)
from capture_splat.reduced_collider import PROBE_REPORT_SCHEMA, REDUCED_REPORT_SCHEMA
from capture_splat.world_studio_export import MANIFEST_NAME, SCHEMA as HANDOFF_SCHEMA


def _ref(path: Path, root: Path) -> dict[str, object]:
    return {
        "path": path.relative_to(root).as_posix(),
        "size_bytes": path.stat().st_size,
        "checksum": f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}",
    }


def _metric_registration() -> dict[str, object]:
    identity = [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0], [0.0, 0.0, 1.0, 0.0], [0.0, 0.0, 0.0, 1.0]]
    return {
        "schema": "capture_splat.metric_registration.v0.1",
        "status": "accepted",
        "accepted": True,
        "source_coordinate_frame": "arkit_world",
        "source_units": "meters",
        "matched_cameras": 3,
        "scale": 1.0,
        "matrix": identity,
        "arkit_to_colmap": identity,
        "authority": {
            "metric_mesh_registration_candidate": True,
            "collision_authority": False,
            "navigation_authority": False,
        },
    }


def _write_handoff(root: Path) -> tuple[Path, dict[str, object]]:
    root.mkdir(parents=True)
    roomplan = root / "room_plan.usdz"
    roomplan.write_bytes(b"observed-roomplan")
    sparse = root / "sparse" / "0"
    sparse.mkdir(parents=True)
    images = sparse / "images.txt"
    images.write_text(
        "1 1 0 0 0 1 0 0 1 a.jpg\n\n"
        "2 1 0 0 0 0 0 0 1 through.jpg\n\n"
        "3 1 0 0 0 -1 0 0 1 b.jpg\n\n",
        encoding="utf-8",
    )
    capture_frames = []
    capture_asset_refs = []
    for frame_index, (frame_id, x) in enumerate((("a", -1.0), ("through", 0.0), ("b", 1.0))):
        paths = {}
        for kind, suffix in (("rgb", ".jpg"), ("depth", ".npy"), ("confidence", ".npy")):
            path = root / kind / f"{frame_id}{suffix}"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"capture-{frame_id}-{kind}".encode())
            paths[kind] = path.relative_to(root).as_posix()
            capture_asset_refs.append(_ref(path, root))
        capture_frames.append({
            **paths,
            "timestamp": frame_index * 0.1,
            "transform_matrix": _pose(x),
            "intrinsics": {
                "fl_x": 500.0,
                "fl_y": 500.0,
                "cx": 320.0,
                "cy": 240.0,
                "w": 640.0,
                "h": 480.0,
            },
        })
    capture_path = root / "capture.json"
    write_json_strict(capture_path, {
        "schema": "capture_splat.v0.3",
        "frames": capture_frames,
    })
    manifest = {
        "schema": HANDOFF_SCHEMA,
        "world_up": [0.0, 1.0, 0.0],
        "world_up_coordinate_frame": "arkit_world",
        "metric_registration": _metric_registration(),
        "assets": {
            "room_plan": {
                **_ref(roomplan, root),
                "coordinate_frame": "roomplan_world_unregistered",
                "units": "meters",
                "authority": "semantic_geometry_proposal",
            },
            "capture_manifest": _ref(capture_path, root),
            "colmap_sparse": {"images.txt": _ref(images, root)},
        },
        "capture_manifest_assets": {
            "schema": "capture_splat.capture_manifest_assets.v0.1",
            "complete": True,
            "decision": "ready",
            "missing": [],
            "conflicts": [],
            "assets": capture_asset_refs,
        },
        "authority": {
            "source_frames": "visual_evidence",
            "trained_splats": "review_proposal",
            "metric_authority": False,
            "collision_authority": False,
            "semantic_authority": False,
            "navigation_authority": False,
            "quality_claim": False,
        },
    }
    path = root / MANIFEST_NAME
    write_json_strict(path, manifest)
    return path, manifest


def _pose(x: float) -> list[list[float]]:
    return [[1.0, 0.0, 0.0, x], [0.0, 1.0, 0.0, 1.0], [0.0, 0.0, 1.0, 0.0], [0.0, 0.0, 0.0, 1.0]]


def _write_evidence(root: Path, handoff_path: Path, handoff: dict[str, object]) -> Path:
    root.mkdir(parents=True)
    provenance = {"capture_id": "room-01-open-01", "producer": "capture-splat-test"}
    free_space = root / "free_space.json"
    write_json_strict(free_space, {
        "schema": FREE_SPACE_SCHEMA,
        "coordinate_frame": "arkit_world",
        "units": "meters",
        "scale_to_meters": 1.0,
        "observed_sample_count": 3,
        "maximum_sample_spacing_meters": 0.6,
        "samples": [
            {
                "position": [x, 1.0, 0.0],
                "horizontal_clearance_meters": 0.45,
                "vertical_clearance_meters": 1.9,
                "support_capture_frame_indices": [index],
            }
            for index, x in enumerate((-1.0, 0.0, 1.0))
        ],
        "uncertainty_meters": 0.03,
        "method": "registered_rgbd_free_samples",
        "provenance": provenance,
        "authority": _false_authority(),
    })
    route = root / "route.json"
    write_json_strict(route, {
        "schema": ROUTE_SCHEMA,
        "coordinate_frame": "arkit_world",
        "units": "meters",
        "scale_to_meters": 1.0,
        "portal_id": "door-01",
        "centerline": [[-1.0, 1.0, 0.0], [0.0, 1.0, 0.0], [1.0, 1.0, 0.0]],
        "corridor_half_width_meters": 0.4,
        "minimum_clear_height_meters": 1.8,
        "uncertainty_meters": 0.03,
        "method": "observed_camera_route_corridor",
        "provenance": provenance,
        "authority": _false_authority(),
    })
    observations = [
        {
            "capture_frame_index": frame_index,
            "registered_image_name": image_name,
            "region": region,
        }
        for frame_index, image_name, region in (
            (0, "a.jpg", "side_a"),
            (1, "through.jpg", "through_opening"),
            (2, "b.jpg", "side_b"),
        )
    ]
    rgbd = root / "registered_rgbd.json"
    registration_digest = _digest_json(handoff["metric_registration"])
    write_json_strict(rgbd, {
        "schema": RGBD_SUPPORT_SCHEMA,
        "coordinate_frame": "arkit_world",
        "units": "meters",
        "scale_to_meters": 1.0,
        "registration_digest": registration_digest,
        "matching": "unique_case_sensitive_rgb_basename_with_same_root_rgb_and_depth_v1",
        "through_band_meters": 0.1,
        "observations": observations,
        "provenance": provenance,
        "authority": _false_authority(),
    })
    closed_handoff = root / "prior" / MANIFEST_NAME
    closed_handoff.parent.mkdir(parents=True)
    write_json_strict(closed_handoff, {
        "schema": HANDOFF_SCHEMA,
        "authority": {
            "source_frames": "visual_evidence",
            "trained_splats": "review_proposal",
            "metric_authority": False,
            "collision_authority": False,
            "semantic_authority": False,
            "navigation_authority": False,
            "quality_claim": False,
        },
    })
    candidate = root / "prior" / "reduced_collider_candidate.ply"
    candidate.write_bytes(b"held-closed-collider")
    candidate_ref = _ref(candidate, root)
    reduced_report = root / "prior" / "capture_splat_reduced_collider_report.json"
    write_json_strict(reduced_report, {
        "schema": REDUCED_REPORT_SCHEMA,
        "decision": "hold",
        "candidate": {**candidate_ref, "triangle_count": 1, "vertex_count": 3},
        "inputs": {
            "hybrid_report": {"path": "capture_splat_hybrid_surface_report.json"},
            "hybrid_surface": {"path": "hybrid_structural_surface.ply"},
            "unsimplified_collider": {"path": "collider_candidate.ply"},
            "unsimplified_collider_report": {
                "path": "capture_splat_hybrid_collider_candidate_report.json"
            },
        },
        "authority": _false_authority(),
    })
    probe_report = root / "prior" / "capture_splat_collision_probe_report.json"
    write_json_strict(probe_report, {
        "schema": PROBE_REPORT_SCHEMA,
        "decision": "hold",
        "inputs": {"reduced_collider": candidate_ref},
        "probes": {"closed_door": {"status": "held"}},
        "authority": _false_authority(),
    })
    handoff_ref = _ref(handoff_path, handoff_path.parent)
    roomplan_asset = handoff["assets"]["room_plan"]
    roomplan_registration = root / "roomplan_registration.json"
    write_json_strict(roomplan_registration, {
        "schema": ROOMPLAN_REGISTRATION_SCHEMA,
        "source_coordinate_frame": "roomplan_world_unregistered",
        "source_roomplan": {
            "size_bytes": roomplan_asset["size_bytes"],
            "checksum": roomplan_asset["checksum"],
        },
        "target_coordinate_frame": "arkit_world",
        "source_units": "meters",
        "target_units": "meters",
        "scale_to_meters": 1.0,
        "transform_to_target": _pose(0.0),
        "registration_uncertainty_meters": 0.03,
        "method": "shared_arkit_session_alignment",
        "provenance": provenance,
        "authority": _false_authority(),
    })
    evidence = {
        "schema": EVIDENCE_SCHEMA,
        "capture_state": "open",
        "source_handoff": {
            "size_bytes": handoff_ref["size_bytes"],
            "checksum": handoff_ref["checksum"],
        },
        "coordinate_contract": {
            "coordinate_frame": "arkit_world",
            "units": "meters",
            "scale_to_meters": 1.0,
            "world_up": [0.0, 1.0, 0.0],
            "position_uncertainty_meters": 0.03,
            "dimension_uncertainty_meters": 0.03,
            "plane_residual_tolerance_meters": 0.01,
        },
        "roomplan": {
            "handoff_asset": {
                "asset_key": "room_plan",
                "size_bytes": roomplan_asset["size_bytes"],
                "checksum": roomplan_asset["checksum"],
            },
            "registration_evidence": _ref(roomplan_registration, root),
        },
        "portal": {
            "id": "door-01",
            "polygon": [[0.0, 0.0, -0.5], [0.0, 2.0, -0.5], [0.0, 2.0, 0.5], [0.0, 0.0, 0.5]],
            "plane": {"normal": [1.0, 0.0, 0.0], "offset_meters": 0.0},
            "clear_width_meters": 1.0,
            "clear_height_meters": 2.0,
            "threshold": {
                "segment": [[0.0, 0.0, -0.5], [0.0, 0.0, 0.5]],
                "height_meters": 0.02,
            },
        },
        "free_space": _ref(free_space, root),
        "route_corridor": _ref(route, root),
        "registered_rgbd_support": _ref(rgbd, root),
        "prior_closed_state_control": {
            "capture_state": "closed",
            "portal_id": "door-01",
            "handoff": _ref(closed_handoff, root),
            "reduced_candidate": candidate_ref,
            "reduced_report": _ref(reduced_report, root),
            "probe_report": _ref(probe_report, root),
        },
        "provenance": provenance,
        "authority": _false_authority(),
    }
    path = root / "portal_route_evidence.json"
    write_json_strict(path, evidence)
    return path


def _fixture(tmp_path: Path) -> tuple[Path, Path]:
    handoff_path, handoff = _write_handoff(tmp_path / "handoff")
    evidence = _write_evidence(tmp_path / "evidence", handoff_path, handoff)
    return handoff_path, evidence


def test_missing_evidence_emits_explicit_held_receipt(tmp_path: Path) -> None:
    handoff_path, _ = _write_handoff(tmp_path / "handoff")

    report = validate_portal_route_evidence(handoff_path, tmp_path / "out")

    assert report["decision"] == "hold"
    assert report["status"] == "held_missing_evidence"
    assert report["outcome"] == {
        "producer_contract_valid": False,
        "evidence_complete_for_future_reduction_design": False,
        "reduction_started": False,
        "traversable": False,
        "collision_candidate_promoted": False,
    }
    assert "registered_rgbd_through_opening" in report["hold_reasons"]
    assert not any(report["authority"].values())


def test_complete_contract_remains_held_and_non_authoritative(tmp_path: Path) -> None:
    handoff, evidence = _fixture(tmp_path)

    report = validate_portal_route_evidence(handoff, tmp_path / "out", evidence=evidence)

    assert report["decision"] == "hold"
    assert report["outcome"]["producer_contract_valid"] is True
    assert report["outcome"]["evidence_complete_for_future_reduction_design"] is True
    assert report["outcome"]["reduction_started"] is False
    assert report["outcome"]["traversable"] is False
    assert report["registered_rgbd_support"]["region_counts"] == {
        "side_a": 1,
        "through_opening": 1,
        "side_b": 1,
    }
    assert not any(report["authority"].values())


def test_receipt_is_deterministic(tmp_path: Path) -> None:
    handoff, evidence = _fixture(tmp_path)

    validate_portal_route_evidence(handoff, tmp_path / "a", evidence=evidence)
    validate_portal_route_evidence(handoff, tmp_path / "b", evidence=evidence)

    assert (tmp_path / "a" / REPORT_NAME).read_bytes() == (tmp_path / "b" / REPORT_NAME).read_bytes()


def test_route_free_space_work_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    handoff, evidence = _fixture(tmp_path)
    monkeypatch.setattr(portal_route_evidence, "_MAX_DISTANCE_EVALUATIONS", 4)

    with pytest.raises(ValueError, match="bounded work limit"):
        validate_portal_route_evidence(handoff, tmp_path / "out", evidence=evidence)


@pytest.mark.parametrize("tamper", ["hash", "path"])
def test_artifact_hash_or_path_tampering_rejects(tmp_path: Path, tamper: str) -> None:
    handoff, evidence = _fixture(tmp_path)
    if tamper == "hash":
        (evidence.parent / "route.json").write_text("{}", encoding="utf-8")
    else:
        payload = load_json_strict(evidence)
        payload["free_space"]["path"] = "../outside.json"
        write_json_strict(evidence, payload)

    with pytest.raises(ValueError):
        validate_portal_route_evidence(handoff, tmp_path / "out", evidence=evidence)

    rejected = load_json_strict(tmp_path / "out" / REPORT_NAME)
    assert rejected["decision"] == "reject"
    assert not any(rejected["authority"].values())


def test_nonfinite_geometry_rejects(tmp_path: Path) -> None:
    handoff, evidence = _fixture(tmp_path)
    payload = load_json_strict(evidence)
    payload["portal"]["clear_width_meters"] = float("nan")
    evidence.write_text(json.dumps(payload, allow_nan=True), encoding="utf-8")

    with pytest.raises(ValueError, match="invalid JSON constant"):
        validate_portal_route_evidence(handoff, tmp_path / "out", evidence=evidence)


def test_invalid_portal_geometry_rejects(tmp_path: Path) -> None:
    handoff, evidence = _fixture(tmp_path)
    payload = load_json_strict(evidence)
    payload["portal"]["polygon"][2][0] = 0.5
    write_json_strict(evidence, payload)

    with pytest.raises(ValueError, match="does not lie"):
        validate_portal_route_evidence(handoff, tmp_path / "out", evidence=evidence)


@pytest.mark.parametrize(
    ("location", "value", "message"),
    [
        (("coordinate_contract", "scale_to_meters"), 2.0, "scale must be one"),
        (("coordinate_contract", "coordinate_frame"), "colmap_world", "arkit_world meters"),
    ],
)
def test_frame_or_scale_mismatch_rejects(
    tmp_path: Path,
    location: tuple[str, str],
    value: object,
    message: str,
) -> None:
    handoff, evidence = _fixture(tmp_path)
    payload = load_json_strict(evidence)
    payload[location[0]][location[1]] = value
    write_json_strict(evidence, payload)

    with pytest.raises(ValueError, match=message):
        validate_portal_route_evidence(handoff, tmp_path / "out", evidence=evidence)


def test_false_authority_rejects(tmp_path: Path) -> None:
    handoff, evidence = _fixture(tmp_path)
    payload = load_json_strict(evidence)
    payload["authority"]["navigation_authority"] = True
    write_json_strict(evidence, payload)

    with pytest.raises(ValueError, match="unsupported authority"):
        validate_portal_route_evidence(handoff, tmp_path / "out", evidence=evidence)


def test_symlinked_evidence_rejects(tmp_path: Path) -> None:
    handoff, evidence = _fixture(tmp_path)
    link = tmp_path / "portal-route-link.json"
    link.symlink_to(evidence)

    with pytest.raises(ValueError, match="symbolic link"):
        validate_portal_route_evidence(handoff, tmp_path / "out", evidence=link)
