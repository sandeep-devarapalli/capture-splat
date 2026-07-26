from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .gsplat_runner import run_gsplat
from .json_utils import load_json_strict, write_json_strict
from .ply_stats import inspect_ply, sanitize_ply_drop_non_finite
from .vksplat_ladder import DEFAULT_STEPS, parse_steps


def _mean_metric(qa_summary: dict[str, Any], name: str) -> float | None:
    metrics = qa_summary.get("aggregates") or qa_summary.get("aggregate_metrics") or {}
    value = metrics.get(name, {}).get("mean")
    return float(value) if isinstance(value, (float, int)) else None


def _find_qa_summary(qa_summary_dir: Path | None, step: int) -> dict[str, Any] | None:
    if qa_summary_dir is None:
        return None
    root = qa_summary_dir.resolve()
    candidates = [
        root / f"step_{step:07d}" / "capture_splat_render_source_qa_summary.json",
        root / f"step_{step:07d}.json",
        root / f"{step}.json",
        root / str(step) / "capture_splat_render_source_qa_summary.json",
    ]
    for candidate in candidates:
        if candidate.exists():
            return load_json_strict(candidate)
    return None


def _regression_reasons(previous: dict[str, Any] | None, current: dict[str, Any] | None, max_psnr_drop: float, max_ssim_drop: float, max_mae_increase: float, max_correlation_drop: float) -> list[str]:
    if previous is None or current is None:
        return []
    reasons = []
    prev_psnr = _mean_metric(previous, "psnr")
    curr_psnr = _mean_metric(current, "psnr")
    if prev_psnr is not None and curr_psnr is not None and curr_psnr < prev_psnr - max_psnr_drop:
        reasons.append("mean_psnr_regressed")
    prev_ssim = _mean_metric(previous, "ssim")
    curr_ssim = _mean_metric(current, "ssim")
    if prev_ssim is not None and curr_ssim is not None and curr_ssim < prev_ssim - max_ssim_drop:
        reasons.append("mean_ssim_regressed")
    prev_mae = _mean_metric(previous, "mae")
    curr_mae = _mean_metric(current, "mae")
    if prev_mae is not None and curr_mae is not None and curr_mae > prev_mae + max_mae_increase:
        reasons.append("mean_mae_regressed")
    prev_corr = _mean_metric(previous, "normalized_correlation")
    curr_corr = _mean_metric(current, "normalized_correlation")
    if prev_corr is not None and curr_corr is not None and curr_corr < prev_corr - max_correlation_drop:
        reasons.append("mean_correlation_regressed")
    return reasons


def run_gsplat_ladder(package_dir: Path, out_dir: Path, gsplat_root: Path, steps: list[int] | None = None, qa_summary_dir: Path | None = None, image_dir: str = "images", sparse_dir: str = "sparse/0", strategy: str = "mcmc", data_factor: int = 1, dry_run: bool = False, sanitize_non_finite_ply: bool = False, max_psnr_drop: float = 0.5, max_ssim_drop: float = 0.02, max_mae_increase: float = 0.01, max_correlation_drop: float = 0.03, photometric: str | None = None, masks: str = "auto", normalization: str = "auto", mcmc_refine_every: str | int = "auto") -> dict[str, Any]:
    package_dir = package_dir.resolve()
    out_dir = out_dir.resolve()
    gsplat_root = gsplat_root.resolve()
    step_values = steps or list(DEFAULT_STEPS)
    out_dir.mkdir(parents=True, exist_ok=True)
    rungs: list[dict[str, Any]] = []
    previous_qa: dict[str, Any] | None = None
    ladder_decision = "hold"
    stop_reason = None

    for step in step_values:
        rung_dir = out_dir / f"step_{step:07d}"
        rung: dict[str, Any] = {
            "step": step,
            "package_dir": str(package_dir),
            "output_dir": str(rung_dir),
            "decision": "hold",
            "reasons": [],
        }
        try:
            run_summary = run_gsplat(package_dir, rung_dir, gsplat_root, steps=step, strategy=strategy, image_dir=image_dir, sparse_dir=sparse_dir, data_factor=data_factor, dry_run=dry_run, photometric=photometric, masks=masks, normalization=normalization, mcmc_refine_every=mcmc_refine_every)
            rung["command"] = run_summary.get("command")
            rung["run_summary"] = run_summary
            splat_path = run_summary.get("splat_ply")
            if dry_run:
                rung["reasons"].append("dry_run_no_ply_validation")
            elif isinstance(splat_path, str):
                splat = Path(splat_path)
                ply_stats = inspect_ply(splat)
                rung["splat_ply"] = splat_path
                rung["ply_stats"] = ply_stats
                rung["finite_ply"] = bool(ply_stats["finite"])
                rung["splat_count"] = ply_stats["splat_count"]
                rung["radius_summary"] = ply_stats["radius_summary"]
                if not ply_stats["finite"] and sanitize_non_finite_ply:
                    sanitize_report = sanitize_ply_drop_non_finite(splat, splat.with_name("point_cloud.finite_drop_nonfinite.ply"))
                    sanitized_stats = sanitize_report["output_ply_stats"]
                    rung["original_splat_ply"] = splat_path
                    rung["original_ply_stats"] = ply_stats
                    rung["splat_ply"] = sanitize_report["output"]
                    rung["ply_stats"] = sanitized_stats
                    rung["finite_ply"] = bool(sanitized_stats["finite"])
                    rung["splat_count"] = sanitized_stats["splat_count"]
                    rung["radius_summary"] = sanitized_stats["radius_summary"]
                    rung["ply_sanitize_report"] = sanitize_report
                    rung["reasons"].append("non_finite_ply_sanitized")
                if not rung["finite_ply"]:
                    rung["decision"] = "reject"
                    rung["reasons"].append("non_finite_ply")
            else:
                rung["finite_ply"] = False
                rung["decision"] = "reject"
                rung["reasons"].append("missing_splat_ply")

            qa_summary = _find_qa_summary(qa_summary_dir, step)
            if qa_summary is not None:
                rung["qa_summary"] = qa_summary
                if qa_summary.get("decision") == "reject":
                    rung["decision"] = "reject"
                    rung["reasons"].append("qa_rejected")
                elif qa_summary.get("decision") == "hold" and rung["decision"] != "reject":
                    rung["decision"] = "hold"
                    rung["reasons"].append("qa_held")
                regression_reasons = _regression_reasons(previous_qa, qa_summary, max_psnr_drop, max_ssim_drop, max_mae_increase, max_correlation_drop)
                if regression_reasons:
                    rung["decision"] = "reject"
                    rung["reasons"].extend(regression_reasons)
                if rung["decision"] != "reject" and qa_summary.get("decision") == "promote" and rung.get("finite_ply", not dry_run):
                    rung["decision"] = "promote"
                    previous_qa = qa_summary
                elif rung["decision"] != "reject":
                    previous_qa = qa_summary
            elif not dry_run and rung["decision"] != "reject":
                rung["reasons"].append("finite_output_without_render_source_qa")
            if not rung["reasons"] and rung["decision"] == "hold":
                rung["reasons"].append("no_quality_gate_promoted")
        except Exception as exc:
            rung["decision"] = "reject"
            rung["error"] = str(exc)
            rung["reasons"].append("runner_error")

        rungs.append(rung)
        if rung["decision"] == "reject":
            ladder_decision = "reject"
            stop_reason = f"step_{step:07d}_rejected"
            break
        if rung["decision"] == "promote":
            ladder_decision = "promote"

    summary = {
        "schema": "capture_splat.gsplat_ladder_summary.v0.1",
        "package_dir": str(package_dir),
        "output_dir": str(out_dir),
        "gsplat_root": str(gsplat_root),
        "steps": step_values,
        "strategy": strategy,
        "data_factor": data_factor,
        "photometric": photometric or "auto",
        "masks": masks,
        "normalization": normalization,
        "mcmc_refine_every": str(mcmc_refine_every),
        "dry_run": dry_run,
        "sanitize_non_finite_ply": sanitize_non_finite_ply,
        "thresholds": {
            "max_psnr_drop": max_psnr_drop,
            "max_ssim_drop": max_ssim_drop,
            "max_mae_increase": max_mae_increase,
            "max_correlation_drop": max_correlation_drop,
        },
        "qa_summary_dir": str(qa_summary_dir.resolve()) if qa_summary_dir else None,
        "decision": ladder_decision,
        "stop_reason": stop_reason,
        "rungs": rungs,
    }
    write_json_strict(out_dir / "capture_splat_gsplat_ladder_summary.json", summary)
    return summary


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a conservative gsplat CUDA quality ladder.")
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--gsplat-root", type=Path, required=True)
    parser.add_argument("--steps", default=",".join(str(step) for step in DEFAULT_STEPS))
    parser.add_argument("--qa-summary-dir", type=Path)
    parser.add_argument("--image-dir", default="images")
    parser.add_argument("--sparse-dir", default="sparse/0")
    parser.add_argument("--strategy", choices=["default", "mcmc"], default="mcmc")
    parser.add_argument("--data-factor", type=int, default=1)
    parser.add_argument("--photometric", choices=["none", "bilateral-grid", "ppisp"])
    parser.add_argument("--masks", choices=["auto", "off", "required"], default="auto")
    parser.add_argument("--normalization", choices=["auto", "on", "off"], default="auto")
    parser.add_argument("--mcmc-refine-every", default="auto", metavar="auto|N")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--sanitize-non-finite-ply", action="store_true")
    parser.add_argument("--max-psnr-drop", type=float, default=0.5)
    parser.add_argument("--max-ssim-drop", type=float, default=0.02)
    parser.add_argument("--max-mae-increase", type=float, default=0.01)
    parser.add_argument("--max-correlation-drop", type=float, default=0.03)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    summary = run_gsplat_ladder(args.package, args.out, args.gsplat_root, steps=parse_steps(args.steps), qa_summary_dir=args.qa_summary_dir, image_dir=args.image_dir, sparse_dir=args.sparse_dir, strategy=args.strategy, data_factor=args.data_factor, dry_run=args.dry_run, sanitize_non_finite_ply=args.sanitize_non_finite_ply, max_psnr_drop=args.max_psnr_drop, max_ssim_drop=args.max_ssim_drop, max_mae_increase=args.max_mae_increase, max_correlation_drop=args.max_correlation_drop, photometric=args.photometric, masks=args.masks, normalization=args.normalization, mcmc_refine_every=args.mcmc_refine_every)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
