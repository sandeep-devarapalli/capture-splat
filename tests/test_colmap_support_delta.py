from pathlib import Path

from capture_splat.colmap_support_delta import compare_colmap_support_delta
from capture_splat.json_utils import load_json_strict, write_json_strict


def write_images_txt(path: Path, rows: list[tuple[int, str, list[int]]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# images"]
    for image_id, name, point_ids in rows:
        lines.append(f"{image_id} 1 0 0 0 0 0 0 1 {name}")
        values = []
        for index, point_id in enumerate(point_ids):
            values.extend([str(index), str(index), str(point_id)])
        lines.append(" ".join(values))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_colmap_support_delta_promotes_when_focus_support_improves(tmp_path: Path) -> None:
    original = tmp_path / "original" / "images.txt"
    repaired = tmp_path / "repaired" / "images.txt"
    write_images_txt(original, [(1, "000065.jpg", [1, -1, -1, -1])])
    write_images_txt(repaired, [(1, "000065.jpg", [1, 2, 3, -1])])

    summary = compare_colmap_support_delta(
        original,
        repaired,
        tmp_path / "out",
        frames="000065",
        min_observation_gain=2,
        min_ratio_gain=0.3,
        require_all_improved=True,
    )
    loaded = load_json_strict(tmp_path / "out" / "capture_splat_colmap_support_delta.json")

    assert summary["decision"] == "proceed_to_training_probe"
    assert loaded["frames"][0]["decision"] == "support_improved"
    assert loaded["frames"][0]["delta_observation_count"] == 2
    assert loaded["authority"]["quality_claim"] is False


def test_colmap_support_delta_holds_on_regression(tmp_path: Path) -> None:
    original = tmp_path / "original" / "images.txt"
    repaired = tmp_path / "repaired" / "images.txt"
    write_images_txt(original, [(1, "000076.jpg", [1, 2, 3, -1])])
    write_images_txt(repaired, [(1, "000076.jpg", [1, -1, -1, -1])])

    summary = compare_colmap_support_delta(original, repaired, tmp_path / "out", frames="000076")

    assert summary["decision"] == "hold"
    assert summary["regressed_count"] == 1
    assert summary["frames"][0]["decision"] == "support_regressed"


def test_colmap_support_delta_can_read_focus_frames_from_weak_report(tmp_path: Path) -> None:
    original = tmp_path / "original" / "images.txt"
    repaired = tmp_path / "repaired" / "images.txt"
    write_images_txt(original, [(1, "000086.jpg", [1, -1, -1])])
    write_images_txt(repaired, [(1, "000086.jpg", [1, 2, -1])])
    weak_report = tmp_path / "weak.json"
    write_json_strict(weak_report, {
        "schema": "capture_splat.weak_frames_report.v0.1",
        "frames": [{"frame_id": "000086"}],
    })

    summary = compare_colmap_support_delta(
        original,
        repaired,
        tmp_path / "out",
        weak_report=weak_report,
        min_observation_gain=1,
        min_ratio_gain=0.2,
    )

    assert summary["frame_count"] == 1
    assert summary["frames"][0]["frame"] == "000086"
    assert summary["decision"] == "proceed_to_training_probe"
