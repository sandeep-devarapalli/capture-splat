from pathlib import Path

from capture_splat.json_utils import load_json_strict, write_json_strict
from capture_splat.reconstruction_recipe import plan_reconstruction, resolve_recipe
from tests.test_capture_schema import make_capture


def _set_capture_metadata(capture_dir: Path, **values: object) -> None:
    path = capture_dir / "capture.json"
    data = load_json_strict(path)
    data.update(values)
    write_json_strict(path, data)


def test_recipe_resolves_desk_intent_and_reports_missing_video(tmp_path: Path) -> None:
    capture = make_capture(tmp_path / "capture")
    _set_capture_metadata(capture, schema="capture_splat.v0.3", capture_intent="scene_cluster")

    summary = plan_reconstruction(capture, tmp_path / "plan")

    assert summary["recipe"]["name"] == "desk"
    assert summary["recipe"]["target_frames"] == 300
    assert summary["recipe"]["matcher"] == "retrieval"
    assert summary["decision"] == "hold"
    assert "continuous_video_missing" in summary["blockers"]


def test_recipe_uses_optional_capture_assets(tmp_path: Path) -> None:
    capture = make_capture(tmp_path / "capture")
    (capture / "video").mkdir()
    (capture / "video/capture.mov").write_bytes(b"video")
    (capture / "metadata").mkdir()
    (capture / "metadata/frame_index.jsonl").write_text("{}\n", encoding="utf-8")
    (capture / "metadata/person_mask_index.jsonl").write_text("{}\n", encoding="utf-8")
    _set_capture_metadata(
        capture,
        schema="capture_splat.v0.3",
        capture_intent="full_room_semantic",
        video_file="video/capture.mov",
        frame_index_file="metadata/frame_index.jsonl",
        person_mask_index_file="metadata/person_mask_index.jsonl",
    )

    summary = plan_reconstruction(capture, tmp_path / "plan")

    assert summary["recipe"]["name"] == "semantic_room"
    assert summary["recipe"]["background_sphere"] is True
    assert summary["recipe"]["person_masks"] is True
    assert summary["decision"] == "ready"


def test_explicit_repair_recipe_wins_over_capture_metadata() -> None:
    name, source = resolve_recipe({"capture_intent": "room_walkthrough"}, "repair")
    assert (name, source) == ("repair", "explicit")
