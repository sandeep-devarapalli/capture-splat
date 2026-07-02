from __future__ import annotations

import argparse
import json
from pathlib import Path

from .app_output_compare import compare_app_outputs
from .capture_quality_report import run_capture_quality_report
from .colmap_export import export_colmap_text
from .ingest import ingest_capture
from .render_source_qa import run_render_source_qa
from .transforms_import import import_transforms_package
from .vksplat_ladder import parse_steps, run_vksplat_ladder
from .vksplat_runner import doctor, run_vksplat


def main() -> None:
    parser = argparse.ArgumentParser(prog="capture-splat")
    sub = parser.add_subparsers(dest="command", required=True)
    p_ingest = sub.add_parser("ingest", help="Normalize capture export")
    p_ingest.add_argument("--capture", type=Path, required=True)
    p_ingest.add_argument("--out", type=Path, required=True)
    p_import = sub.add_parser("import-transforms", help="Convert Nerfstudio/Record3D-style transforms exports into a Capture Splat package")
    p_import.add_argument("--input", type=Path, required=True)
    p_import.add_argument("--out", type=Path, required=True)
    p_import.add_argument("--no-copy-files", action="store_true")
    p_import.add_argument("--require-depth", action="store_true")
    p_colmap = sub.add_parser("colmap-export", help="Write COLMAP text package")
    p_colmap.add_argument("--capture", type=Path, required=True)
    p_colmap.add_argument("--out", type=Path, required=True)
    p_capture_quality = sub.add_parser("capture-quality-report", help="Summarize capture-time quality before training")
    p_capture_quality.add_argument("--capture", type=Path, required=True)
    p_capture_quality.add_argument("--out", type=Path, required=True)
    p_capture_quality.add_argument("--min-accepted-frames", type=int, default=24)
    p_capture_quality.add_argument("--min-mean-blur-score", type=float, default=0.006)
    p_capture_quality.add_argument("--min-mean-parallax-meters", type=float, default=0.05)
    p_capture_quality.add_argument("--min-mean-overlap-score", type=float, default=0.45)
    p_capture_quality.add_argument("--min-mean-depth-ratio", type=float, default=0.35)
    p_compare = sub.add_parser("compare-app-output", help="Compare observable outputs from iPhone 3DGS apps")
    p_compare.add_argument("--capture-splat", type=Path)
    p_compare.add_argument("--splatking", type=Path)
    p_compare.add_argument("--kiri", type=Path)
    p_compare.add_argument("--out", type=Path, required=True)
    p_train = sub.add_parser("train-vksplat", help="Run VkSplat on a COLMAP package")
    p_train.add_argument("--package", type=Path, required=True)
    p_train.add_argument("--out", type=Path, required=True)
    p_train.add_argument("--vksplat-root", type=Path, required=True)
    p_train.add_argument("--steps", type=int, default=30000)
    p_train.add_argument("--dry-run", action="store_true")
    p_qa = sub.add_parser("qa-render-source", help="Compare render canvases against source images")
    p_qa.add_argument("--source-dir", type=Path, required=True)
    p_qa.add_argument("--render-dir", type=Path, required=True)
    p_qa.add_argument("--out", type=Path, required=True)
    p_qa.add_argument("--pairs-json", type=Path)
    p_qa.add_argument("--min-psnr", type=float, default=20.0)
    p_qa.add_argument("--min-ssim", type=float, default=0.85)
    p_qa.add_argument("--max-mae", type=float, default=0.08)
    p_qa.add_argument("--min-correlation", type=float, default=0.75)
    p_qa.add_argument("--tail-fraction", type=float, default=0.25)
    p_ladder = sub.add_parser("train-vksplat-ladder", help="Run controlled VkSplat training rungs")
    p_ladder.add_argument("--package", type=Path, required=True)
    p_ladder.add_argument("--out", type=Path, required=True)
    p_ladder.add_argument("--vksplat-root", type=Path, required=True)
    p_ladder.add_argument("--steps", default="3000,7000,15000,30000")
    p_ladder.add_argument("--qa-summary-dir", type=Path)
    p_ladder.add_argument("--image-dir", default="images")
    p_ladder.add_argument("--sparse-dir", default="sparse/0")
    p_ladder.add_argument("--strategy", choices=["default", "mcmc"], default="mcmc")
    p_ladder.add_argument("--dry-run", action="store_true")
    p_ladder.add_argument("--max-psnr-drop", type=float, default=0.5)
    p_ladder.add_argument("--max-ssim-drop", type=float, default=0.02)
    p_ladder.add_argument("--max-mae-increase", type=float, default=0.01)
    p_ladder.add_argument("--max-correlation-drop", type=float, default=0.03)
    p_doctor = sub.add_parser("doctor", help="Check local runtime tools")
    p_doctor.add_argument("--vksplat-root", type=Path)
    args = parser.parse_args()
    if args.command == "ingest":
        payload = ingest_capture(args.capture, args.out)
    elif args.command == "import-transforms":
        payload = import_transforms_package(
            args.input,
            args.out,
            copy_files=not args.no_copy_files,
            require_depth=args.require_depth,
        )
    elif args.command == "colmap-export":
        payload = export_colmap_text(args.capture, args.out)
    elif args.command == "capture-quality-report":
        payload = run_capture_quality_report(
            args.capture,
            args.out,
            min_accepted_frames=args.min_accepted_frames,
            min_mean_blur_score=args.min_mean_blur_score,
            min_mean_parallax_meters=args.min_mean_parallax_meters,
            min_mean_overlap_score=args.min_mean_overlap_score,
            min_mean_depth_ratio=args.min_mean_depth_ratio,
        )
    elif args.command == "compare-app-output":
        payload = compare_app_outputs(
            args.out,
            capture_splat=args.capture_splat,
            splatking=args.splatking,
            kiri=args.kiri,
        )
    elif args.command == "train-vksplat":
        payload = run_vksplat(args.package, args.out, args.vksplat_root, steps=args.steps, dry_run=args.dry_run)
    elif args.command == "qa-render-source":
        payload = run_render_source_qa(
            args.source_dir,
            args.render_dir,
            args.out,
            pairs_json=args.pairs_json,
            min_psnr=args.min_psnr,
            min_ssim=args.min_ssim,
            max_mae=args.max_mae,
            min_correlation=args.min_correlation,
            tail_fraction=args.tail_fraction,
        )
    elif args.command == "train-vksplat-ladder":
        payload = run_vksplat_ladder(
            args.package,
            args.out,
            args.vksplat_root,
            steps=parse_steps(args.steps),
            qa_summary_dir=args.qa_summary_dir,
            image_dir=args.image_dir,
            sparse_dir=args.sparse_dir,
            strategy=args.strategy,
            dry_run=args.dry_run,
            max_psnr_drop=args.max_psnr_drop,
            max_ssim_drop=args.max_ssim_drop,
            max_mae_increase=args.max_mae_increase,
            max_correlation_drop=args.max_correlation_drop,
        )
    elif args.command == "doctor":
        payload = doctor(args.vksplat_root)
    else:
        raise AssertionError(args.command)
    print(json.dumps(payload, indent=2))
