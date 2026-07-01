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
