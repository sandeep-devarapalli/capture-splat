from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from .app_output_compare import compare_app_outputs
from .backend_render_compare import compare_backend_renders
from .capture_quality_report import run_capture_quality_report
from .colmap_export import export_colmap_text
from .frames_extract import run_extract_frames
from .colmap_focused_repair import run_colmap_focused_repair
from .colmap_support_delta import compare_colmap_support_delta
from .colmap_support_repair import build_colmap_support_repair
from .ingest import ingest_capture
from .ply_stats import prune_ply_by_alpha, sanitize_ply_drop_non_finite
from .prepare_capture import prepare_capture
from .render_source_qa import run_render_source_qa
from .reconstruction_recipe import RECIPES, plan_reconstruction
from .scene_transform import write_scene_transform_sidecar
from .sfm_runner import colmap_has_cuda, run_sfm, run_triangulate
from .transforms_import import import_transforms_package
from .gsplat_ladder import run_gsplat_ladder
from .gsplat_runner import doctor as gsplat_doctor
from .gsplat_runner import run_gsplat
from .hloc_runner import hloc_status
from .vksplat_ladder import parse_steps, run_vksplat_ladder
from .weak_frames_report import run_weak_frames_report
from .vksplat_runner import doctor as vksplat_doctor
from .vksplat_runner import run_vksplat
from .vksplat_render_probe import run_vksplat_render_probe
from .world_studio_export import export_world_studio_handoff


def _external_source_status(root: Path | None, required_files: list[str]) -> dict[str, object]:
    if root is None:
        return {"source_present": False}
    root = root.resolve()
    result: dict[str, object] = {"source_present": root.exists(), "root": str(root)}
    if root.exists():
        result["required_files_present"] = all((root / name).exists() for name in required_files)
        result["build_dir_present"] = (root / "build").exists()
    return result


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
    p_plan = sub.add_parser("plan-reconstruction", help="Resolve an intent-aware host reconstruction recipe")
    p_plan.add_argument("--capture", type=Path, required=True)
    p_plan.add_argument("--out", type=Path, required=True)
    p_plan.add_argument("--recipe", choices=["auto", *RECIPES], default="auto")
    p_prepare = sub.add_parser("prepare-capture", help="Prepare RGB-D-first frames and video supplements for SfM")
    p_prepare.add_argument("--capture", type=Path, required=True)
    p_prepare.add_argument("--out", type=Path, required=True)
    p_prepare.add_argument("--recipe", choices=["auto", *RECIPES], default="auto")
    p_prepare.add_argument("--target-frames", type=int)
    p_prepare.add_argument("--max-edge", type=int, default=1920)
    p_prepare.add_argument("--dedup-tolerance", type=float, default=0.08)
    p_compare = sub.add_parser("compare-app-output", help="Compare observable outputs from iPhone 3DGS apps")
    p_compare.add_argument("--capture-splat", type=Path)
    p_compare.add_argument("--splatking", type=Path)
    p_compare.add_argument("--kiri", type=Path)
    p_compare.add_argument("--out", type=Path, required=True)
    p_compare_backends = sub.add_parser("compare-backend-renders", help="Compare backend renders against one shared source frame list")
    p_compare_backends.add_argument("--package", type=Path, required=True)
    p_compare_backends.add_argument("--out", type=Path, required=True)
    p_compare_backends.add_argument("--frames", help="Comma-separated frame ids or filenames")
    p_compare_backends.add_argument("--frames-json", type=Path)
    p_compare_backends.add_argument("--gsplat-ply", type=Path)
    p_compare_backends.add_argument("--vksplat-ply", type=Path)
    p_compare_backends.add_argument("--gsplat-render-dir", type=Path)
    p_compare_backends.add_argument("--vksplat-render-dir", type=Path)
    p_compare_backends.add_argument("--gsplat-renderer-command")
    p_compare_backends.add_argument("--vksplat-renderer-command")
    p_compare_backends.add_argument("--image-dir", default="images")
    p_world_studio = sub.add_parser("export-world-studio", help="Write a Capture Splat handoff package for World Studio")
    p_world_studio.add_argument("--package", type=Path, required=True)
    p_world_studio.add_argument("--out", type=Path, required=True)
    p_world_studio.add_argument("--gaussian", type=Path)
    p_world_studio.add_argument("--points", type=Path)
    p_world_studio.add_argument("--capture-manifest", type=Path)
    p_world_studio.add_argument("--transforms", type=Path)
    p_world_studio.add_argument("--poses", type=Path)
    p_world_studio.add_argument("--camera-poses", type=Path)
    p_world_studio.add_argument("--splat", type=Path)
    p_world_studio.add_argument("--spz", type=Path)
    p_world_studio.add_argument("--image-dir", default="images")
    p_world_studio.add_argument("--sparse-dir", default="sparse/0")
    p_world_studio.add_argument("--copy-files", action="store_true")
    p_world_studio.add_argument("--capture-profile", choices=["object", "room_interior", "walkthrough", "outdoor", "video_360"])
    p_train = sub.add_parser("train-vksplat", help="Run VkSplat on a COLMAP package")
    p_train.add_argument("--package", type=Path, required=True)
    p_train.add_argument("--out", type=Path, required=True)
    p_train.add_argument("--vksplat-root", type=Path, required=True)
    p_train.add_argument("--steps", type=int, default=30000)
    p_train.add_argument("--save-train-renders", action="store_true")
    p_train.add_argument("--stop-reset-at", type=int, help="Stop VkSplat opacity resets after this step.")
    p_train.add_argument("--dry-run", action="store_true")
    p_probe = sub.add_parser("vksplat-render-probe", help="Train VkSplat with train renders enabled and QA exact source-frame cameras")
    p_probe.add_argument("--package", type=Path, required=True)
    p_probe.add_argument("--out", type=Path, required=True)
    p_probe.add_argument("--vksplat-root", type=Path, required=True)
    p_probe.add_argument("--frames", help="Comma-separated frame ids or filenames")
    p_probe.add_argument("--steps", type=int, default=7000)
    p_probe.add_argument("--image-dir", default="images")
    p_probe.add_argument("--sparse-dir", default="sparse/0")
    p_probe.add_argument("--strategy", choices=["default", "mcmc"], default="mcmc")
    p_probe.add_argument("--dry-run", action="store_true")
    p_train_gsplat = sub.add_parser("train-gsplat", help="Run gsplat CUDA training on a COLMAP package")
    p_train_gsplat.add_argument("--package", type=Path, required=True)
    p_train_gsplat.add_argument("--out", type=Path, required=True)
    p_train_gsplat.add_argument("--gsplat-root", type=Path, required=True)
    p_train_gsplat.add_argument("--steps", type=int, default=30000)
    p_train_gsplat.add_argument("--strategy", choices=["default", "mcmc"], default="mcmc")
    p_train_gsplat.add_argument("--image-dir", default="images")
    p_train_gsplat.add_argument("--sparse-dir", default="sparse/0")
    p_train_gsplat.add_argument("--data-factor", type=int, default=1)
    p_train_gsplat.add_argument("--no-bilateral-grid", action="store_true")
    p_train_gsplat.add_argument("--no-random-bkgd", action="store_true")
    p_train_gsplat.add_argument("--max-gaussians", type=int, default=1_000_000)
    p_train_gsplat.add_argument("--dry-run", action="store_true")
    p_scene_transform = sub.add_parser("scene-transform", help="Write the scene transform sidecar next to a trained PLY")
    p_scene_transform.add_argument("--ply", type=Path, required=True)
    p_scene_transform.add_argument("--sparse-dir", type=Path)
    p_scene_transform.add_argument("--trainer", choices=["gsplat", "vksplat"], default="gsplat")
    p_scene_transform.add_argument("--no-normalize", action="store_true")
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
    p_sanitize = sub.add_parser("sanitize-ply", help="Drop non-finite PLY vertices and write a strict report")
    p_sanitize.add_argument("--input", type=Path, required=True)
    p_sanitize.add_argument("--out", type=Path)
    p_frames = sub.add_parser("extract-frames", help="Extract training frames from a capture video")
    p_frames.add_argument("--video", type=Path, required=True)
    p_frames.add_argument("--out", type=Path, required=True)
    p_frames.add_argument("--target-frames", type=int, default=300)
    p_frames.add_argument("--max-edge", type=int, default=1920)
    p_frames.add_argument("--pick", choices=["sharpest", "first"], default="sharpest")
    p_frames.add_argument("--frame-index", type=Path)
    p_sfm = sub.add_parser("sfm", help="Run COLMAP/GLOMAP SfM and produce an orientation-aligned package")
    p_sfm.add_argument("--images", type=Path, required=True)
    p_sfm.add_argument("--out", type=Path, required=True)
    p_sfm.add_argument("--method", choices=["colmap", "glomap"], default="colmap")
    p_sfm.add_argument("--matcher", choices=["sequential", "exhaustive", "retrieval"], default="exhaustive")
    p_sfm.add_argument("--features", choices=["sift", "hloc"], default="sift")
    p_sfm.add_argument("--retrieval-top-k", type=int, default=32)
    p_sfm.add_argument("--overlap", type=int, default=30)
    p_sfm.add_argument("--no-loop-detection", action="store_true")
    p_sfm.add_argument("--vocab-tree", type=Path)
    p_sfm.add_argument("--max-features", type=int, default=8192)
    p_sfm.add_argument("--no-copy-images", action="store_true")
    p_sfm.add_argument("--background-sphere", action="store_true")
    p_sfm.add_argument("--allow-cpu-matching", action="store_true", help="Run without CUDA COLMAP; recorded in the summary as cpu_matching_override")
    p_sfm.add_argument("--dry-run", action="store_true")
    p_triangulate = sub.add_parser("triangulate", help="Triangulate a device-pose package with COLMAP and align orientation")
    p_triangulate.add_argument("--package", type=Path, required=True)
    p_triangulate.add_argument("--out", type=Path, required=True)
    p_triangulate.add_argument("--overlap", type=int, default=30)
    p_triangulate.add_argument("--loop-detection", action="store_true")
    p_triangulate.add_argument("--vocab-tree", type=Path)
    p_triangulate.add_argument("--max-features", type=int, default=8192)
    p_triangulate.add_argument("--refine-poses", action="store_true")
    p_triangulate.add_argument("--background-sphere", action="store_true")
    p_triangulate.add_argument("--allow-cpu-matching", action="store_true", help="Run without CUDA COLMAP; recorded in the summary as cpu_matching_override")
    p_triangulate.add_argument("--dry-run", action="store_true")
    p_prune = sub.add_parser("prune-ply", help="Drop near-transparent splats below an alpha threshold for viewer hygiene")
    p_prune.add_argument("--input", type=Path, required=True)
    p_prune.add_argument("--out", type=Path)
    p_prune.add_argument("--min-alpha", type=float, default=12.0, help="Keep splats with sigmoid(opacity)*255 >= this value")
    p_prune.add_argument("--max-dropped-fraction", type=float, default=0.6)
    p_weak = sub.add_parser("qa-weak-frames-report", help="Diagnose weak render/source QA frames")
    p_weak.add_argument("--qa-summary", type=Path, required=True)
    p_weak.add_argument("--out", type=Path, required=True)
    p_weak.add_argument("--colmap-images", type=Path)
    p_weak.add_argument("--capture", type=Path)
    p_weak.add_argument("--min-colmap-observations", type=int, default=100)
    p_weak.add_argument("--min-colmap-observation-ratio", type=float, default=0.10)
    p_weak.add_argument("--min-blur-score", type=float, default=0.006)
    p_weak.add_argument("--min-parallax-meters", type=float, default=0.05)
    p_weak.add_argument("--min-overlap-score", type=float, default=0.45)
    p_weak.add_argument("--max-clipped-fraction", type=float, default=0.02)
    p_weak.add_argument("--max-contact-frames", type=int, default=12)
    p_repair = sub.add_parser("colmap-support-repair", help="Build a targeted COLMAP support repair manifest")
    p_repair.add_argument("--weak-report", type=Path, required=True)
    p_repair.add_argument("--package", type=Path, required=True)
    p_repair.add_argument("--out", type=Path, required=True)
    p_repair.add_argument("--capture", type=Path)
    p_repair.add_argument("--colmap-images", type=Path)
    p_repair.add_argument("--image-dir", default="images")
    p_repair.add_argument("--neighbor-radius", type=int, default=4)
    p_repair.add_argument("--max-anchors-per-target", type=int, default=8)
    p_repair.add_argument("--min-colmap-observations", type=int, default=100)
    p_repair.add_argument("--min-colmap-observation-ratio", type=float, default=0.10)
    p_delta = sub.add_parser("colmap-support-delta", help="Compare COLMAP observation support before and after a repair pass")
    p_delta.add_argument("--original-images", type=Path, required=True)
    p_delta.add_argument("--repaired-images", type=Path, required=True)
    p_delta.add_argument("--out", type=Path, required=True)
    p_delta.add_argument("--frames")
    p_delta.add_argument("--weak-report", type=Path)
    p_delta.add_argument("--min-observation-gain", type=int, default=100)
    p_delta.add_argument("--min-ratio-gain", type=float, default=0.03)
    p_delta.add_argument("--require-all-improved", action="store_true")
    p_focused_repair = sub.add_parser("colmap-focused-repair", help="Run a focused COLMAP repair workspace for weak frames")
    p_focused_repair.add_argument("--package", type=Path, required=True)
    p_focused_repair.add_argument("--out", type=Path, required=True)
    p_focused_repair.add_argument("--weak-report", type=Path)
    p_focused_repair.add_argument("--repair-manifest", type=Path)
    p_focused_repair.add_argument("--image-dir", default="images")
    p_focused_repair.add_argument("--sparse-dir", default="sparse/0")
    p_focused_repair.add_argument("--neighbor-radius", type=int, default=4)
    p_focused_repair.add_argument("--max-anchors-per-target", type=int, default=8)
    p_focused_repair.add_argument("--max-num-features", type=int, default=16384)
    p_focused_repair.add_argument("--use-gpu", action="store_true")
    p_focused_repair.add_argument("--include-all-registered-images", action="store_true")
    p_focused_repair.add_argument("--bridge-ranges")
    p_focused_repair.add_argument("--bridge-window", type=int, default=6)
    p_focused_repair.add_argument("--preserve-existing-points", action="store_true")
    p_focused_repair.add_argument("--dry-run", action="store_true")
    p_focused_repair.add_argument("--colmap-binary", default="colmap")
    p_gsplat_ladder = sub.add_parser("train-gsplat-ladder", help="Run controlled gsplat CUDA training rungs")
    p_gsplat_ladder.add_argument("--package", type=Path, required=True)
    p_gsplat_ladder.add_argument("--out", type=Path, required=True)
    p_gsplat_ladder.add_argument("--gsplat-root", type=Path, required=True)
    p_gsplat_ladder.add_argument("--steps", default="3000,7000,15000,30000")
    p_gsplat_ladder.add_argument("--qa-summary-dir", type=Path)
    p_gsplat_ladder.add_argument("--image-dir", default="images")
    p_gsplat_ladder.add_argument("--sparse-dir", default="sparse/0")
    p_gsplat_ladder.add_argument("--strategy", choices=["default", "mcmc"], default="mcmc")
    p_gsplat_ladder.add_argument("--data-factor", type=int, default=1)
    p_gsplat_ladder.add_argument("--dry-run", action="store_true")
    p_gsplat_ladder.add_argument("--sanitize-non-finite-ply", action="store_true")
    p_gsplat_ladder.add_argument("--max-psnr-drop", type=float, default=0.5)
    p_gsplat_ladder.add_argument("--max-ssim-drop", type=float, default=0.02)
    p_gsplat_ladder.add_argument("--max-mae-increase", type=float, default=0.01)
    p_gsplat_ladder.add_argument("--max-correlation-drop", type=float, default=0.03)
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
    p_ladder.add_argument("--sanitize-non-finite-ply", action="store_true")
    p_ladder.add_argument("--stop-reset-at", type=int, help="Stop VkSplat opacity resets after this step for every rung.")
    p_ladder.add_argument("--max-psnr-drop", type=float, default=0.5)
    p_ladder.add_argument("--max-ssim-drop", type=float, default=0.02)
    p_ladder.add_argument("--max-mae-increase", type=float, default=0.01)
    p_ladder.add_argument("--max-correlation-drop", type=float, default=0.03)
    p_doctor = sub.add_parser("doctor", help="Check local runtime tools")
    p_doctor.add_argument("--vksplat-root", type=Path)
    p_doctor.add_argument("--gsplat-root", type=Path)
    p_doctor.add_argument("--three-dgs-cpp-root", type=Path)
    p_doctor.add_argument("--andrew-3dgs-root", type=Path)
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
    elif args.command == "plan-reconstruction":
        payload = plan_reconstruction(args.capture, args.out, recipe=args.recipe)
    elif args.command == "prepare-capture":
        payload = prepare_capture(
            args.capture,
            args.out,
            recipe=args.recipe,
            target_frames=args.target_frames,
            max_edge=args.max_edge,
            dedup_tolerance_seconds=args.dedup_tolerance,
        )
    elif args.command == "compare-app-output":
        payload = compare_app_outputs(
            args.out,
            capture_splat=args.capture_splat,
            splatking=args.splatking,
            kiri=args.kiri,
        )
    elif args.command == "compare-backend-renders":
        payload = compare_backend_renders(
            args.package,
            args.out,
            frames=args.frames,
            frames_json=args.frames_json,
            gsplat_ply=args.gsplat_ply,
            vksplat_ply=args.vksplat_ply,
            gsplat_render_dir=args.gsplat_render_dir,
            vksplat_render_dir=args.vksplat_render_dir,
            gsplat_renderer_command=args.gsplat_renderer_command,
            vksplat_renderer_command=args.vksplat_renderer_command,
            image_dir_name=args.image_dir,
        )
    elif args.command == "export-world-studio":
        payload = export_world_studio_handoff(
            args.package,
            args.out,
            gaussian=args.gaussian,
            points=args.points,
            capture_manifest=args.capture_manifest,
            transforms=args.transforms,
            poses=args.poses,
            camera_poses=args.camera_poses,
            splat=args.splat,
            spz=args.spz,
            image_dir_name=args.image_dir,
            sparse_dir_name=args.sparse_dir,
            copy_files=args.copy_files,
            capture_profile=args.capture_profile,
        )
    elif args.command == "train-vksplat":
        payload = run_vksplat(args.package, args.out, args.vksplat_root, steps=args.steps, dry_run=args.dry_run, save_train_renders=args.save_train_renders, stop_reset_at=args.stop_reset_at)
    elif args.command == "vksplat-render-probe":
        payload = run_vksplat_render_probe(
            args.package,
            args.out,
            args.vksplat_root,
            frames=args.frames,
            steps=args.steps,
            image_dir=args.image_dir,
            sparse_dir=args.sparse_dir,
            strategy=args.strategy,
            dry_run=args.dry_run,
        )
    elif args.command == "train-gsplat":
        payload = run_gsplat(
            args.package,
            args.out,
            args.gsplat_root,
            steps=args.steps,
            strategy=args.strategy,
            image_dir=args.image_dir,
            sparse_dir=args.sparse_dir,
            data_factor=args.data_factor,
            dry_run=args.dry_run,
            use_bilateral_grid=not args.no_bilateral_grid,
            random_bkgd=not args.no_random_bkgd,
            max_gaussians=args.max_gaussians,
        )
    elif args.command == "scene-transform":
        payload = write_scene_transform_sidecar(args.ply, args.sparse_dir, args.trainer, normalized=not args.no_normalize)
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
    elif args.command == "sanitize-ply":
        payload = sanitize_ply_drop_non_finite(args.input, args.out)
    elif args.command == "extract-frames":
        payload = run_extract_frames(
            args.video,
            args.out,
            target_frames=args.target_frames,
            max_edge=args.max_edge,
            pick=args.pick,
            frame_index=args.frame_index,
        )
    elif args.command == "sfm":
        payload = run_sfm(
            args.images,
            args.out,
            method=args.method,
            matcher=args.matcher,
            features=args.features,
            retrieval_top_k=args.retrieval_top_k,
            overlap=args.overlap,
            loop_detection=not args.no_loop_detection,
            vocab_tree=args.vocab_tree,
            max_features=args.max_features,
            copy_images=not args.no_copy_images,
            background_sphere=args.background_sphere,
            allow_cpu_matching=args.allow_cpu_matching,
            dry_run=args.dry_run,
        )
    elif args.command == "triangulate":
        payload = run_triangulate(
            args.package,
            args.out,
            overlap=args.overlap,
            loop_detection=args.loop_detection,
            vocab_tree=args.vocab_tree,
            max_features=args.max_features,
            refine_poses=args.refine_poses,
            background_sphere=args.background_sphere,
            allow_cpu_matching=args.allow_cpu_matching,
            dry_run=args.dry_run,
        )
    elif args.command == "prune-ply":
        payload = prune_ply_by_alpha(args.input, args.out, min_alpha=args.min_alpha, max_dropped_fraction=args.max_dropped_fraction)
    elif args.command == "qa-weak-frames-report":
        payload = run_weak_frames_report(
            args.qa_summary,
            args.out,
            colmap_images=args.colmap_images,
            capture=args.capture,
            min_colmap_observations=args.min_colmap_observations,
            min_colmap_observation_ratio=args.min_colmap_observation_ratio,
            min_blur_score=args.min_blur_score,
            min_parallax_meters=args.min_parallax_meters,
            min_overlap_score=args.min_overlap_score,
            max_clipped_fraction=args.max_clipped_fraction,
            max_contact_frames=args.max_contact_frames,
        )
    elif args.command == "colmap-support-repair":
        payload = build_colmap_support_repair(
            args.weak_report,
            args.package,
            args.out,
            capture=args.capture,
            colmap_images=args.colmap_images,
            image_dir_name=args.image_dir,
            neighbor_radius=args.neighbor_radius,
            max_anchors_per_target=args.max_anchors_per_target,
            min_colmap_observations=args.min_colmap_observations,
            min_colmap_observation_ratio=args.min_colmap_observation_ratio,
        )
    elif args.command == "colmap-support-delta":
        payload = compare_colmap_support_delta(
            args.original_images,
            args.repaired_images,
            args.out,
            frames=args.frames,
            weak_report=args.weak_report,
            min_observation_gain=args.min_observation_gain,
            min_ratio_gain=args.min_ratio_gain,
            require_all_improved=args.require_all_improved,
        )
    elif args.command == "colmap-focused-repair":
        payload = run_colmap_focused_repair(
            args.package,
            args.out,
            weak_report=args.weak_report,
            repair_manifest=args.repair_manifest,
            image_dir_name=args.image_dir,
            sparse_dir_name=args.sparse_dir,
            neighbor_radius=args.neighbor_radius,
            max_anchors_per_target=args.max_anchors_per_target,
            max_num_features=args.max_num_features,
            use_gpu=args.use_gpu,
            include_all_registered_images=args.include_all_registered_images,
            bridge_ranges=args.bridge_ranges,
            bridge_window=args.bridge_window,
            preserve_existing_points=args.preserve_existing_points,
            dry_run=args.dry_run,
            colmap_binary=args.colmap_binary,
        )
    elif args.command == "train-gsplat-ladder":
        payload = run_gsplat_ladder(
            args.package,
            args.out,
            args.gsplat_root,
            steps=parse_steps(args.steps),
            qa_summary_dir=args.qa_summary_dir,
            image_dir=args.image_dir,
            sparse_dir=args.sparse_dir,
            strategy=args.strategy,
            data_factor=args.data_factor,
            dry_run=args.dry_run,
            sanitize_non_finite_ply=args.sanitize_non_finite_ply,
            max_psnr_drop=args.max_psnr_drop,
            max_ssim_drop=args.max_ssim_drop,
            max_mae_increase=args.max_mae_increase,
            max_correlation_drop=args.max_correlation_drop,
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
            sanitize_non_finite_ply=args.sanitize_non_finite_ply,
            stop_reset_at=args.stop_reset_at,
            max_psnr_drop=args.max_psnr_drop,
            max_ssim_drop=args.max_ssim_drop,
            max_mae_increase=args.max_mae_increase,
            max_correlation_drop=args.max_correlation_drop,
        )
    elif args.command == "doctor":
        payload = {
            "schema": "capture_splat.doctor.v0.4",
            "tools": {name: shutil.which(name) for name in ("colmap", "glomap", "ffmpeg", "ffprobe")},
            "colmap_cuda": colmap_has_cuda(),
            "hloc": hloc_status(),
            "vksplat": vksplat_doctor(args.vksplat_root),
            "gsplat": gsplat_doctor(args.gsplat_root),
            "three_dgs_cpp": _external_source_status(args.three_dgs_cpp_root, ["CMakeLists.txt"]),
            "andrew_3dgs": _external_source_status(args.andrew_3dgs_root, ["CMakeLists.txt"]),
        }
    else:
        raise AssertionError(args.command)
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
