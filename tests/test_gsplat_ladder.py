from pathlib import Path

from capture_splat.gsplat_ladder import run_gsplat_ladder
from capture_splat.json_utils import write_json_strict


def make_package(root: Path) -> Path:
    package = root / "package"
    (package / "images").mkdir(parents=True)
    sparse = package / "sparse" / "0"
    sparse.mkdir(parents=True)
    (package / "images" / "000001.jpg").write_bytes(b"fixture")
    (sparse / "cameras.txt").write_text("# cameras\n", encoding="utf-8")
    (sparse / "images.txt").write_text("# images\n", encoding="utf-8")
    return package


def make_gsplat_root(root: Path) -> Path:
    gsplat = root / "gsplat"
    examples = gsplat / "examples"
    examples.mkdir(parents=True)
    (examples / "simple_trainer.py").write_text("print('fake')\n", encoding="utf-8")
    return gsplat


def write_qa(path: Path, psnr: float, ssim: float, mae: float, correlation: float, decision: str = "promote") -> None:
    write_json_strict(path, {
        "schema": "capture_splat.render_source_qa.v0.1",
        "decision": decision,
        "aggregates": {
            "psnr": {"mean": psnr, "min": psnr, "max": psnr},
            "ssim": {"mean": ssim, "min": ssim, "max": ssim},
            "mae": {"mean": mae, "min": mae, "max": mae},
            "normalized_correlation": {"mean": correlation, "min": correlation, "max": correlation},
        },
    })


def test_gsplat_ladder_dry_run_rejects_regression(tmp_path: Path) -> None:
    package = make_package(tmp_path)
    gsplat = make_gsplat_root(tmp_path)
    qa = tmp_path / "qa"
    qa.mkdir()
    write_qa(qa / "step_0003000.json", 24.0, 0.95, 0.02, 0.95)
    write_qa(qa / "step_0007000.json", 20.0, 0.90, 0.04, 0.90)

    summary = run_gsplat_ladder(package, tmp_path / "out", gsplat, steps=[3000, 7000], qa_summary_dir=qa, dry_run=True)

    assert summary["schema"] == "capture_splat.gsplat_ladder_summary.v0.1"
    assert summary["decision"] == "reject"
    assert summary["stop_reason"] == "step_0007000_rejected"
    assert "mean_psnr_regressed" in summary["rungs"][1]["reasons"]
    assert (tmp_path / "out" / "capture_splat_gsplat_ladder_summary.json").exists()


def test_gsplat_ladder_records_fake_ply_stats_and_promotes(tmp_path: Path, monkeypatch) -> None:
    package = make_package(tmp_path)
    gsplat = make_gsplat_root(tmp_path)
    qa = tmp_path / "qa"
    qa.mkdir()
    write_qa(qa / "step_0003000.json", 24.0, 0.95, 0.02, 0.95)

    def fake_run_gsplat(package_dir, output_root, gsplat_root, **kwargs):
        ply = output_root / "ply" / "point_cloud_3000.ply"
        ply.parent.mkdir(parents=True)
        ply.write_text(
            "\n".join([
                "ply",
                "format ascii 1.0",
                "element vertex 2",
                "property float x",
                "property float y",
                "property float z",
                "property float scale_0",
                "property float scale_1",
                "property float scale_2",
                "end_header",
                "0 0 0 0 0 0",
                "1 2 3 0.4 0.5 0.6",
            ]) + "\n",
            encoding="ascii",
        )
        return {"command": ["fake"], "splat_ply": str(ply), "returncode": 0}

    monkeypatch.setattr("capture_splat.gsplat_ladder.run_gsplat", fake_run_gsplat)

    summary = run_gsplat_ladder(package, tmp_path / "out", gsplat, steps=[3000], qa_summary_dir=qa)

    rung = summary["rungs"][0]
    assert summary["decision"] == "promote"
    assert rung["decision"] == "promote"
    assert rung["finite_ply"] is True
    assert rung["splat_count"] == 2
    assert rung["radius_summary"]["count"] == 6
