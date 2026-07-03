from pathlib import Path

from capture_splat.json_utils import write_json_strict
from capture_splat.vksplat_ladder import run_vksplat_ladder


def make_package(root: Path) -> Path:
    package = root / "package"
    (package / "images").mkdir(parents=True)
    sparse = package / "sparse" / "0"
    sparse.mkdir(parents=True)
    (package / "images" / "000001.jpg").write_bytes(b"fixture")
    (sparse / "cameras.txt").write_text("# cameras\n", encoding="utf-8")
    (sparse / "images.txt").write_text("# images\n", encoding="utf-8")
    return package


def make_vksplat_root(root: Path) -> Path:
    vksplat = root / "vksplat"
    vksplat.mkdir()
    (vksplat / "simple_trainer.py").write_text("class MCMCTrainerConfig: pass\nclass TrainerConfig: pass\ndef train_main(config): pass\n", encoding="utf-8")
    return vksplat


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


def test_ladder_dry_run_records_commands_and_rejects_regression(tmp_path: Path) -> None:
    package = make_package(tmp_path)
    vksplat = make_vksplat_root(tmp_path)
    qa = tmp_path / "qa"
    qa.mkdir()
    write_qa(qa / "step_0003000.json", 24.0, 0.95, 0.02, 0.95)
    write_qa(qa / "step_0007000.json", 20.0, 0.90, 0.04, 0.90)

    summary = run_vksplat_ladder(
        package,
        tmp_path / "out",
        vksplat,
        steps=[3000, 7000, 15000],
        qa_summary_dir=qa,
        dry_run=True,
        max_psnr_drop=0.5,
    )

    assert summary["decision"] == "reject"
    assert summary["stop_reason"] == "step_0007000_rejected"
    assert len(summary["rungs"]) == 2
    assert summary["rungs"][0]["command"]
    assert summary["rungs"][0]["decision"] == "hold"
    assert "mean_psnr_regressed" in summary["rungs"][1]["reasons"]
    assert (tmp_path / "out" / "capture_splat_vksplat_ladder_summary.json").exists()


def test_ladder_can_use_sanitized_finite_ply_with_promoting_qa(tmp_path: Path, monkeypatch) -> None:
    package = make_package(tmp_path)
    vksplat = make_vksplat_root(tmp_path)
    qa = tmp_path / "qa"
    qa.mkdir()
    write_qa(qa / "step_0003000.json", 24.0, 0.95, 0.02, 0.95)

    def fake_run_vksplat(package_dir, output_root, vksplat_root, **kwargs):
        splat = output_root / "run" / "splat.ply"
        splat.parent.mkdir(parents=True)
        splat.write_text(
            "\n".join([
                "ply",
                "format ascii 1.0",
                "element vertex 3",
                "property float x",
                "property float y",
                "property float z",
                "property float scale_0",
                "property float scale_1",
                "property float scale_2",
                "end_header",
                "0 0 0 0 0 0",
                "nan 1 2 0.1 0.2 0.3",
                "1 2 3 0.4 0.5 0.6",
            ]) + "\n",
            encoding="ascii",
        )
        return {"command": ["fake"], "splat_ply": str(splat), "returncode": 0}

    monkeypatch.setattr("capture_splat.vksplat_ladder.run_vksplat", fake_run_vksplat)

    summary = run_vksplat_ladder(
        package,
        tmp_path / "out",
        vksplat,
        steps=[3000],
        qa_summary_dir=qa,
        sanitize_non_finite_ply=True,
    )

    rung = summary["rungs"][0]
    assert summary["decision"] == "promote"
    assert rung["decision"] == "promote"
    assert rung["finite_ply"] is True
    assert rung["original_ply_stats"]["finite"] is False
    assert rung["ply_stats"]["finite"] is True
    assert rung["ply_sanitize_report"]["dropped_vertex_count"] == 1
    assert "non_finite_ply_sanitized" in rung["reasons"]
