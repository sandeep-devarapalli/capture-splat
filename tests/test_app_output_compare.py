from pathlib import Path

import pytest

from capture_splat.app_output_compare import compare_app_outputs
from capture_splat.json_utils import load_json_strict, write_json_strict


def write_ascii_ply(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join([
            "ply",
            "format ascii 1.0",
            "element vertex 1",
            "property float x",
            "property float y",
            "property float z",
            "property float scale_0",
            "property float scale_1",
            "property float scale_2",
            "end_header",
            "0 0 0 0 0 0",
        ]) + "\n",
        encoding="ascii",
    )


def test_compare_app_outputs_summarizes_observable_artifacts(tmp_path: Path) -> None:
    capture_splat = tmp_path / "capture_splat"
    splatking = tmp_path / "splatking"
    write_ascii_ply(capture_splat / "splat.ply")
    write_json_strict(capture_splat / "capture_splat_render_source_qa_summary.json", {
        "schema": "capture_splat.render_source_qa.v0.1",
        "decision": "promote",
    })
    splatking.mkdir()
    (splatking / "scene.ksplat").write_bytes(b"fixture")

    summary = compare_app_outputs(tmp_path / "out", capture_splat=capture_splat, splatking=splatking)
    loaded = load_json_strict(tmp_path / "out" / "capture_splat_app_output_comparison.json")

    assert summary["comparison"]["app_count"] == 2
    assert loaded["apps"][0]["artifacts"][0]["finite"] is True
    assert loaded["apps"][0]["render_source_qa_summary"]["decision"] == "promote"
    assert loaded["apps"][1]["artifacts"][0]["suffix"] == ".ksplat"
    assert loaded["authority"]["observable_artifacts_only"] is True


def test_compare_app_outputs_rejects_missing_artifacts(tmp_path: Path) -> None:
    capture_splat = tmp_path / "capture_splat"
    kiri = tmp_path / "kiri"
    capture_splat.mkdir()
    kiri.mkdir()
    (kiri / "notes.txt").write_text("no export yet\n", encoding="utf-8")

    summary = compare_app_outputs(tmp_path / "out", capture_splat=capture_splat, kiri=kiri)

    assert summary["decision"] == "hold"
    assert "one_or_more_app_outputs_missing_3d_artifacts" in summary["warnings"]
    assert all(app["decision"] == "reject" for app in summary["apps"])


def test_compare_app_outputs_requires_two_inputs(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="at least two"):
        compare_app_outputs(tmp_path / "out", capture_splat=tmp_path / "capture_splat")
