import struct
from pathlib import Path

import pytest

from capture_splat.json_utils import load_json_strict
from capture_splat.ply_stats import inspect_ply, prune_ply_by_alpha

HEADER_PROPERTIES = [
    "property float x",
    "property float y",
    "property float z",
    "property float opacity",
]


def write_ascii_ply(path: Path, rows: list[str], properties: list[str] | None = None) -> None:
    path.write_text(
        "\n".join([
            "ply",
            "format ascii 1.0",
            f"element vertex {len(rows)}",
            *(properties or HEADER_PROPERTIES),
            "end_header",
            *rows,
        ]) + "\n",
        encoding="ascii",
    )


def write_binary_ply(path: Path, rows: list[tuple[float, float, float, float]]) -> None:
    header = "\n".join([
        "ply",
        "format binary_little_endian 1.0",
        f"element vertex {len(rows)}",
        *HEADER_PROPERTIES,
        "end_header",
    ]) + "\n"
    with path.open("wb") as handle:
        handle.write(header.encode("ascii"))
        for row in rows:
            handle.write(struct.pack("<ffff", *row))


def test_prune_drops_low_alpha_vertices_and_writes_report(tmp_path: Path) -> None:
    ply = tmp_path / "splat.ply"
    write_ascii_ply(ply, ["0 0 0 3.0", "1 1 1 -5.0", "2 2 2 0.0"])

    report = prune_ply_by_alpha(ply)

    out = tmp_path / "splat.pruned_a12.ply"
    assert report["output"] == str(out)
    assert report["decision"] == "pruned"
    assert report["source_vertex_count"] == 3
    assert report["output_vertex_count"] == 2
    assert report["dropped_vertex_count"] == 1
    assert inspect_ply(out)["splat_count"] == 2
    saved = load_json_strict(out.with_suffix(out.suffix + ".prune_report.json"))
    assert saved["output_ply_stats"]["splat_count"] == 2
    assert sum(bucket["count"] for bucket in saved["alpha_histogram"]) == 3
    assert saved["authority"]["quality_claim"] is False


def test_prune_binary_ply(tmp_path: Path) -> None:
    ply = tmp_path / "splat.ply"
    write_binary_ply(ply, [(0, 0, 0, 3.0), (1, 1, 1, -5.0)])

    report = prune_ply_by_alpha(ply)

    assert report["output_vertex_count"] == 1
    assert inspect_ply(tmp_path / "splat.pruned_a12.ply")["splat_count"] == 1


def test_prune_refuses_when_dropped_fraction_exceeds_limit(tmp_path: Path) -> None:
    ply = tmp_path / "foggy.ply"
    write_ascii_ply(ply, ["0 0 0 -5.0", "1 1 1 -6.0", "2 2 2 3.0"])

    with pytest.raises(RuntimeError, match="refusing to prune"):
        prune_ply_by_alpha(ply)

    out = tmp_path / "foggy.pruned_a12.ply"
    assert not out.exists()
    saved = load_json_strict(out.with_suffix(out.suffix + ".prune_report.json"))
    assert saved["decision"] == "reject"
    assert saved["output"] is None


def test_prune_requires_opacity_property(tmp_path: Path) -> None:
    ply = tmp_path / "plain.ply"
    write_ascii_ply(ply, ["0 0 0"], properties=["property float x", "property float y", "property float z"])

    with pytest.raises(ValueError, match="opacity"):
        prune_ply_by_alpha(ply)


def test_prune_warns_when_opacity_looks_activated(tmp_path: Path) -> None:
    ply = tmp_path / "activated.ply"
    write_ascii_ply(ply, ["0 0 0 0.9", "1 1 1 0.8"])

    report = prune_ply_by_alpha(ply)

    assert "opacity_values_all_within_0_1_may_already_be_activated" in report["warnings"]


def test_prune_can_drop_extreme_radius_splats(tmp_path: Path) -> None:
    ply = tmp_path / "large_radius.ply"
    properties = [
        *HEADER_PROPERTIES,
        "property float scale_0",
        "property float scale_1",
        "property float scale_2",
    ]
    write_ascii_ply(
        ply,
        [
            "0 0 0 3.0 -4.0 -4.0 -4.0",
            "0.5 0.5 0.5 3.0 -3.0 -3.0 -3.0",
            "1 1 1 3.0 -0.2 -4.0 -4.0",
            "2 2 2 -5.0 -0.1 -4.0 -4.0",
        ],
        properties=properties,
    )

    report = prune_ply_by_alpha(ply, max_radius=0.5)

    assert report["method"] == "drop_vertices_below_alpha_or_above_radius_threshold"
    assert report["output_vertex_count"] == 2
    assert report["alpha_dropped_vertex_count"] == 1
    assert report["radius_dropped_vertex_count"] == 2
    assert report["alpha_and_radius_dropped_vertex_count"] == 1
    assert report["output_ply_stats"]["radius_summary"]["max"] < 0.5


def test_prune_max_radius_requires_scale_properties(tmp_path: Path) -> None:
    ply = tmp_path / "plain_splat.ply"
    write_ascii_ply(ply, ["0 0 0 3.0"])

    with pytest.raises(ValueError, match="scale_0"):
        prune_ply_by_alpha(ply, max_radius=0.5)
