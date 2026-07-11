from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from .capture_schema import load_capture
from .json_utils import write_json_strict

SUMMARY_SCHEMA = "capture_splat.reconstruction_plan.v0.1"

RECIPES: dict[str, dict[str, Any]] = {
    "desk": {"target_frames": 300, "matcher": "retrieval", "background_sphere": False, "viewer_preset": "orbit"},
    "object": {"target_frames": 300, "matcher": "retrieval", "background_sphere": False, "viewer_preset": "orbit"},
    "room": {"target_frames": 450, "matcher": "retrieval", "background_sphere": True, "viewer_preset": "inside_tour"},
    "semantic_room": {"target_frames": 450, "matcher": "retrieval", "background_sphere": True, "viewer_preset": "inside_tour"},
    "corridor": {"target_frames": 450, "matcher": "retrieval", "background_sphere": False, "viewer_preset": "inside_tour"},
    "wall": {"target_frames": 300, "matcher": "retrieval", "background_sphere": False, "viewer_preset": "orbit"},
    "outdoor": {"target_frames": 450, "matcher": "retrieval", "background_sphere": True, "viewer_preset": "free_orbit"},
    "repair": {"target_frames": 180, "matcher": "exhaustive", "background_sphere": False, "viewer_preset": "frame_orbit"},
}

INTENT_RECIPES = {
    "scene_cluster": "desk",
    "object_orbit": "object",
    "room_walkthrough": "room",
    "full_room_semantic": "semantic_room",
    "corridor_passage": "corridor",
    "facade_wall": "wall",
    "outdoor_object": "outdoor",
    "detail_repair": "repair",
}

PROFILE_RECIPES = {
    "object": "object",
    "room_interior": "room",
    "walkthrough": "room",
    "outdoor": "outdoor",
    "video_3dgs_max": "desk",
}


def _capture_asset(capture_dir: Path, capture: dict[str, Any], key: str, fallback: str) -> tuple[str, bool]:
    value = capture.get(key)
    relative = value if isinstance(value, str) and value else fallback
    return relative, (capture_dir / relative).exists()


def resolve_recipe(capture: dict[str, Any], requested: str = "auto") -> tuple[str, str]:
    if requested != "auto":
        if requested not in RECIPES:
            raise ValueError(f"unknown reconstruction recipe: {requested}")
        return requested, "explicit"
    intent = capture.get("capture_intent")
    if isinstance(intent, str) and intent in INTENT_RECIPES:
        return INTENT_RECIPES[intent], "capture_intent"
    profile = capture.get("capture_profile")
    if isinstance(profile, str) and profile in PROFILE_RECIPES:
        return PROFILE_RECIPES[profile], "capture_profile"
    return "room", "fallback"


def plan_reconstruction(capture_dir: Path, out_dir: Path, recipe: str = "auto") -> dict[str, Any]:
    capture_dir = capture_dir.resolve()
    out_dir = out_dir.resolve()
    capture = load_capture(capture_dir)
    recipe_name, source = resolve_recipe(capture, recipe)
    resolved = deepcopy(RECIPES[recipe_name])
    video_path, video_present = _capture_asset(capture_dir, capture, "video_file", "video/capture.mov")
    index_path, index_present = _capture_asset(capture_dir, capture, "frame_index_file", "metadata/frame_index.jsonl")
    person_path, person_present = _capture_asset(
        capture_dir, capture, "person_mask_index_file", "metadata/person_mask_index.jsonl"
    )
    mesh_path, mesh_present = _capture_asset(capture_dir, capture, "arkit_mesh_file", "geometry/arkit_mesh.ply")
    resolved.update({
        "name": recipe_name,
        "source": source,
        "capture_intent": capture.get("capture_intent"),
        "capture_profile": capture.get("capture_profile"),
        "frame_source_policy": "accepted_rgbd_then_continuous_video_supplement",
        "matcher_policy": "retrieval_over_250_else_exhaustive",
        "features": "hloc" if resolved["matcher"] == "retrieval" else "sift",
        "sfm_method": "global",
        "object_masks": recipe_name == "object",
        "person_masks": person_present,
        "metric_seed": "arkit_rgbd_if_sim3_gate_passes",
        "training_backend": "vksplat",
        "training_steps": [3000, 7000, 15000, 30000],
        "hold_continues_when_output_is_usable": True,
        "non_finite_ply_rejects": True,
    })
    blockers: list[str] = []
    if not video_present:
        blockers.append("continuous_video_missing")
    if not index_present:
        blockers.append("frame_index_missing")
    summary = {
        "schema": SUMMARY_SCHEMA,
        "capture_dir": str(capture_dir),
        "output_dir": str(out_dir),
        "recipe": resolved,
        "assets": {
            "continuous_video": {"path": video_path, "present": video_present},
            "frame_index": {"path": index_path, "present": index_present},
            "person_mask_index": {"path": person_path, "present": person_present},
            "arkit_mesh": {"path": mesh_path, "present": mesh_present},
        },
        "blockers": blockers,
        "decision": "ready" if not blockers else "hold",
        "authority": {
            "execution_plan_only": True,
            "quality_claim": False,
            "metric_authority": False,
        },
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    write_json_strict(out_dir / "capture_splat_reconstruction_plan.json", summary)
    return summary
