from pathlib import Path

from capture_splat.ply_stats import inspect_ply


def write_ply(path: Path, rows: list[str]) -> None:
    path.write_text(
        "\n".join([
            "ply",
            "format ascii 1.0",
            f"element vertex {len(rows)}",
            "property float x",
            "property float y",
            "property float z",
            "property float scale_0",
            "property float scale_1",
            "property float scale_2",
            "end_header",
            *rows,
        ]) + "\n",
        encoding="ascii",
    )


def test_ascii_ply_reports_finite_scale_and_radius_summary(tmp_path: Path) -> None:
    ply = tmp_path / "splat.ply"
    write_ply(ply, ["0 0 0 0 0 0", "1 2 3 0.1 0.2 0.3"])

    summary = inspect_ply(ply)

    assert summary["finite"] is True
    assert summary["splat_count"] == 2
    assert summary["non_finite_count"] == 0
    assert summary["scale_summary"]["scale_0"]["count"] == 2
    assert summary["radius_summary"]["count"] == 6


def test_ascii_ply_counts_non_finite_values(tmp_path: Path) -> None:
    ply = tmp_path / "bad.ply"
    write_ply(ply, ["0 0 0 0 0 0", "nan 2 3 0.1 inf 0.3"])

    summary = inspect_ply(ply)

    assert summary["finite"] is False
    assert summary["non_finite_count"] == 2
    assert summary["scale_summary"]["scale_1"]["count"] == 1
