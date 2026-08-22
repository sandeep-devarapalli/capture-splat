from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path

from .app_output_compare import compare_app_outputs
from .apriltag_scale import apriltag_status, validate_apriltag_scale
from .background_remove import remove_background
from .backend_render_compare import compare_backend_renders
from .capture_quality_report import run_capture_quality_report
from .collision_candidate import build_collision_candidate
from .colmap_export import export_colmap_text
from .equirectangular_import import import_equirectangular
from .equirectangular_sfm import run_equirectangular_rig_sfm
from .frames_extract import run_extract_frames
from .colmap_focused_repair import run_colmap_focused_repair
from .colmap_support_delta import compare_colmap_support_delta
from .colmap_support_repair import build_colmap_support_repair
from .ingest import ingest_capture
from .live_replay import DEFAULT_LIVE_RECEIVER, replay_live_session
from .ply_stats import prune_ply_by_alpha, sanitize_ply_drop_non_finite
from .prepare_capture import prepare_capture
from .reconstruct import STAGES, reconstruct_capture
from .render_source_qa import run_render_source_qa
from .rgbd_seed import build_rgbd_metric_seed
from .rgbd_tsdf import build_rgbd_tsdf
from .reconstruction_recipe import RECIPES, plan_reconstruction
from .scene_transform import write_scene_transform_sidecar
from .sfm_runner import colmap_capabilities, colmap_has_cuda, run_sfm, run_triangulate
from .spz_export import export_spz
from .transforms_import import import_transforms_package
from .training_supervision import prepare_training_supervision
from .gsplat_ladder import run_gsplat_ladder
from .gsplat_runner import doctor as gsplat_doctor
from .gsplat_runner import run_gsplat
from .hloc_runner import hloc_status
from .hybrid_surface import build_hybrid_surface
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
    p_import_360 = sub.add_parser("import-360", help="Project equirectangular images or video into perspective SfM images")
    p_import_360.add_argument("--input", type=Path, required=True)
    p_import_360.add_argument("--out", type=Path, required=True)
    p_import_360.add_argument("--size", type=int, default=1024)
    p_import_360.add_argument("--fov", type=float, default=110.0)
    p_import_360.add_argument("--target-panoramas", type=int, default=12)
    p_sfm_360 = sub.add_parser("sfm-360-rig", help="Recover panorama poses with a fixed virtual-camera COLMAP rig")
    p_sfm_360.add_argument("--package", type=Path, required=True)
    p_sfm_360.add_argument("--out", type=Path, required=True)
    p_sfm_360.add_argument("--method", choices=["global", "incremental"], default="global")
    p_sfm_360.add_argument("--overlap", type=int, default=30)
    p_sfm_360.add_argument("--max-features", type=int, default=8192)
    p_sfm_360.add_argument("--allow-cpu-matching", action="store_true")
    p_sfm_360.add_argument("--dry-run", action="store_true")
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
    p_prepare.add_argument(
        "--frame-exclusions",
        type=Path,
        help="Strict JSON manifest of accepted source-frame indices to omit non-destructively",
    )
    p_supervision = sub.add_parser(
        "prepare-training-supervision",
        help="Validate metric depth/confidence and derive checksum-bound normal proposals",
    )
    p_supervision.add_argument("--package", type=Path, required=True)
    p_supervision.add_argument("--confidence-minimum", type=int, choices=[0, 1, 2], default=1)
    p_supervision.add_argument("--no-derive-normals", action="store_true")
    p_remove_background = sub.add_parser(
        "remove-background",
        help="Write non-destructive premultiplied object images from valid masks or optional InSPyReNet",
    )
    p_remove_background.add_argument("--images", type=Path, required=True)
    p_remove_background.add_argument("--out", type=Path, required=True)
    p_remove_background.add_argument("--mask-dir", type=Path)
    p_remove_background.add_argument("--mode", choices=["auto", "prior", "inspyrenet"], default="auto")
    p_remove_background.add_argument("--threshold", type=float, default=0.5)
    p_remove_background.add_argument(
        "--model-mode",
        choices=["fast", "base", "base-nightly"],
        default="fast",
    )
    p_remove_background.add_argument("--dry-run", action="store_true")
    p_seed = sub.add_parser("build-rgbd-seed", help="Align ARKit RGB-D to COLMAP and augment a copied package")
    p_seed.add_argument("--capture", type=Path, required=True)
    p_seed.add_argument("--package", type=Path, required=True)
    p_seed.add_argument("--out", type=Path, required=True)
    p_seed.add_argument("--min-cameras", type=int, default=8)
    p_seed.add_argument("--max-median-fraction", type=float, default=0.03)
    p_seed.add_argument("--max-p95-fraction", type=float, default=0.08)
    p_seed.add_argument("--confidence-minimum", type=int, default=1)
    p_seed.add_argument("--voxel-size", type=float, default=0.02)
    p_seed.add_argument("--max-points", type=int, default=250_000)
    p_seed.add_argument("--seed-source", choices=["auto", "depth", "mesh"], default="auto")
    p_tsdf = sub.add_parser(
        "build-rgbd-tsdf",
        help="Fuse checksum-bound registered iPhone RGB-D frames into a held Open3D mesh candidate",
    )
    p_tsdf.add_argument("--handoff", type=Path, required=True)
    p_tsdf.add_argument("--out", type=Path, required=True)
    p_hybrid = sub.add_parser(
        "build-hybrid-surface",
        help="Transfer locally supported ARKit classes onto an immutable TSDF surface candidate",
    )
    p_hybrid.add_argument("--handoff", type=Path, required=True)
    p_hybrid.add_argument("--tsdf-report", type=Path, required=True)
    p_hybrid.add_argument("--out", type=Path, required=True)
    p_hybrid.add_argument("--maximum-distance", type=float, default=0.06)
    p_hybrid.add_argument("--minimum-normal-dot", type=float, default=0.8)
    p_hybrid.add_argument("--ambiguity-epsilon", type=float, default=0.00001)
    p_hybrid.add_argument("--collider-triangle-budget", type=int, default=60_000)
    p_reconstruct = sub.add_parser("reconstruct", help="Run the resumable capture-to-3DGS evidence pipeline")
    p_reconstruct.add_argument("--capture", type=Path, required=True)
    p_reconstruct.add_argument("--out", type=Path, required=True)
    p_reconstruct.add_argument("--backend", choices=["vksplat", "gsplat"], default="vksplat")
    p_reconstruct.add_argument("--backend-root", type=Path)
    p_reconstruct.add_argument("--recipe", choices=["auto", *RECIPES], default="auto")
    p_reconstruct.add_argument("--steps", default="3000,7000,15000,30000")
    p_reconstruct.add_argument("--qa-render-dir", type=Path)
    p_reconstruct.add_argument("--qa-pairs-json", type=Path)
    p_reconstruct.add_argument("--qa-provenance-json", type=Path)
    p_reconstruct.add_argument("--resume", action="store_true")
    p_reconstruct.add_argument("--dry-run", action="store_true")
    p_reconstruct.add_argument("--stop-after", choices=STAGES, default="export")
    p_reconstruct.add_argument("--allow-cpu-matching", action="store_true")
    p_reconstruct.add_argument("--retrieval-top-k", type=int, default=32)
    p_reconstruct.add_argument("--prune-alpha", type=float, default=12.0)
    p_reconstruct.add_argument("--max-pruned-fraction", type=float, default=0.6)
    p_reconstruct.add_argument("--stop-reset-at", type=int)
    p_reconstruct.add_argument("--normalization", choices=["auto", "on", "off"], default="auto")
    p_reconstruct.add_argument("--mcmc-refine-every", default="auto", metavar="auto|N")
    p_reconstruct.add_argument("--seed-source", choices=["auto", "depth", "mesh"], default="auto")
    p_reconstruct.add_argument("--depth-supervision", choices=["auto", "off", "required"], default="auto")
    p_reconstruct.add_argument("--normal-supervision", choices=["auto", "off", "required"], default="auto")
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
    p_world_studio.add_argument("--navigation-mesh", type=Path)
    p_world_studio.add_argument("--mesh-report", type=Path)
    p_world_studio.add_argument("--room-semantics", type=Path)
    p_world_studio.add_argument("--camera-trajectory", type=Path)
    p_world_studio.add_argument("--planes", type=Path)
    p_world_studio.add_argument("--metric-scale-report", type=Path)
    p_world_studio.add_argument("--known-scale-report", type=Path)
    p_world_studio.add_argument("--collision-candidate", type=Path)
    p_world_studio.add_argument("--collision-report", type=Path)
    p_world_studio.add_argument("--render-source-qa", type=Path)
    p_world_studio.add_argument("--measurement-points", type=Path)
    p_world_studio.add_argument(
        "--measurement-points-frame",
        choices=["arkit_world", "colmap_world", "metric_colmap_world", "trainer_world"],
        default="colmap_world",
    )
    p_world_studio.add_argument("--image-dir", default="images")
    p_world_studio.add_argument("--sparse-dir", default="sparse/0")
    p_world_studio.add_argument("--copy-files", action="store_true")
    p_world_studio.add_argument("--capture-profile", choices=["object", "room_interior", "walkthrough", "outdoor", "video_360"])
    p_live_replay = sub.add_parser(
        "replay-live-session",
        help="Replay an existing capture to a loopback World Studio live receiver",
    )
    p_live_replay.add_argument("--capture", type=Path, required=True)
    p_live_replay.add_argument(
        "--receiver",
        default=os.environ.get("CAPTURE_SPLAT_LIVE_RECEIVER", DEFAULT_LIVE_RECEIVER),
    )
    p_live_replay.add_argument("--session-id")
    p_live_replay.add_argument("--delay-ms", type=int, default=0)
    p_live_replay.add_argument("--shuffle", action="store_true")
    p_live_replay.add_argument("--seed", type=int, default=0)
    p_live_replay.add_argument("--duplicate-every", type=int, default=0)
    p_live_replay.add_argument("--disconnect-after", type=int)
    p_live_replay.add_argument("--disconnect-seconds", type=float, default=0.0)
    p_live_replay.add_argument("--resume", action="store_true")
    p_train = sub.add_parser("train-vksplat", help="Run VkSplat on a COLMAP package")
    p_train.add_argument("--package", type=Path, required=True)
    p_train.add_argument("--out", type=Path, required=True)
    p_train.add_argument("--vksplat-root", type=Path, required=True)
    p_train.add_argument("--steps", type=int, default=30000)
    p_train.add_argument("--save-train-renders", action="store_true")
    p_train.add_argument("--stop-reset-at", type=int, help="Stop VkSplat opacity resets after this step.")
    p_train.add_argument("--masks", choices=["auto", "off", "required"], default="auto")
    p_train.add_argument("--normalization", choices=["auto", "on", "off"], default="auto")
    p_train.add_argument("--depth-supervision", choices=["auto", "off", "required"], default="auto")
    p_train.add_argument("--normal-supervision", choices=["auto", "off", "required"], default="auto")
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
    p_probe.add_argument("--normalization", choices=["auto", "on", "off"], default="auto")
    p_train_gsplat = sub.add_parser("train-gsplat", help="Run gsplat CUDA training on a COLMAP package")
    p_train_gsplat.add_argument("--package", type=Path, required=True)
    p_train_gsplat.add_argument("--out", type=Path, required=True)
    p_train_gsplat.add_argument("--gsplat-root", type=Path, required=True)
    p_train_gsplat.add_argument("--steps", type=int, default=30000)
    p_train_gsplat.add_argument("--strategy", choices=["default", "mcmc"], default="mcmc")
    p_train_gsplat.add_argument("--image-dir", default="images")
    p_train_gsplat.add_argument("--sparse-dir", default="sparse/0")
    p_train_gsplat.add_argument("--data-factor", type=int, default=1)
    p_train_gsplat.add_argument("--photometric", choices=["none", "bilateral-grid", "ppisp"])
    p_train_gsplat.add_argument("--masks", choices=["auto", "off", "required"], default="auto")
    p_train_gsplat.add_argument("--normalization", choices=["auto", "on", "off"], default="auto")
    p_train_gsplat.add_argument("--depth-supervision", choices=["auto", "off", "required"], default="auto")
    p_train_gsplat.add_argument("--normal-supervision", choices=["auto", "off", "required"], default="auto")
    p_train_gsplat.add_argument("--no-bilateral-grid", action="store_true")
    p_train_gsplat.add_argument("--no-random-bkgd", action="store_true")
    p_train_gsplat.add_argument("--max-gaussians", type=int, default=1_000_000)
    p_train_gsplat.add_argument("--mcmc-refine-every", default="auto", metavar="auto|N")
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
    p_spz = sub.add_parser("export-spz", help="Convert a finite Gaussian PLY to SPZ with strict round-trip evidence")
    p_spz.add_argument("--input", type=Path, required=True)
    p_spz.add_argument("--out", type=Path, required=True)
    p_spz.add_argument("--converter", type=Path)
    p_spz.add_argument("--viewer-evidence", type=Path)
    p_spz.add_argument("--sample-limit", type=int, default=50_000)
    p_spz.add_argument("--max-position-p95-fraction", type=float, default=0.005)
    p_spz.add_argument("--max-color-mae", type=float, default=0.03)
    p_spz.add_argument("--dry-run", action="store_true")
    p_collision = sub.add_parser(
        "build-collision-candidate",
        help="Simplify classified ARKit mesh evidence without granting collision authority",
    )
    p_collision.add_argument("--mesh", type=Path, required=True)
    p_collision.add_argument("--mesh-report", type=Path, required=True)
    p_collision.add_argument("--out", type=Path, required=True)
    p_collision.add_argument("--max-faces", type=int, default=100_000)
    p_collision.add_argument("--cell-size", type=float, default=0.5)
    p_collision.add_argument("--intent", choices=["room", "object"], default="room")
    p_apriltag = sub.add_parser(
        "validate-apriltag-scale",
        help="Validate COLMAP metric scale from a measured AprilTag without modifying the package",
    )
    p_apriltag.add_argument("--package", type=Path, required=True)
    p_apriltag.add_argument("--out", type=Path, required=True)
    p_apriltag.add_argument("--tag-size-meters", type=float, required=True)
    p_apriltag.add_argument("--detections-json", type=Path)
    p_apriltag.add_argument("--artifact", type=Path)
    p_apriltag.add_argument("--family", default="tagStandard41h12")
    p_apriltag.add_argument("--min-views", type=int, default=3)
    p_apriltag.add_argument("--max-reprojection-p95", type=float, default=3.0)
    p_apriltag.add_argument("--max-edge-cv", type=float, default=0.15)
    p_apriltag.add_argument("--max-scale-error-fraction", type=float, default=0.05)
    p_apriltag.add_argument("--image-dir", default="images")
    p_apriltag.add_argument("--sparse-dir", default="sparse/0")
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
    p_sfm.add_argument("--method", choices=["global", "incremental", "glomap", "colmap"], default="global")
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
    p_sfm.add_argument("--camera-policy", choices=["auto", "per-frame", "single"], default="auto")
    p_sfm.add_argument("--view-graph-calibration", choices=["auto", "on", "off"], default="auto")
    p_sfm.add_argument("--masks", choices=["auto", "off", "required"], default="auto")
    p_sfm.add_argument("--post-ba-backend", choices=["none", "ceres", "caspar"], default="none")
    p_sfm.add_argument("--capture-manifest", type=Path)
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
    p_prune = sub.add_parser("prune-ply", help="Drop near-transparent or extreme-radius splats for viewer hygiene")
    p_prune.add_argument("--input", type=Path, required=True)
    p_prune.add_argument("--out", type=Path)
    p_prune.add_argument("--min-alpha", type=float, default=12.0, help="Keep splats with sigmoid(opacity)*255 >= this value")
    p_prune.add_argument(
        "--max-radius",
        type=float,
        help="Optionally keep splats whose largest exp(scale_0..2) radius is at most this many trainer-scene units",
    )
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
    p_gsplat_ladder.add_argument("--photometric", choices=["none", "bilateral-grid", "ppisp"])
    p_gsplat_ladder.add_argument("--masks", choices=["auto", "off", "required"], default="auto")
    p_gsplat_ladder.add_argument("--normalization", choices=["auto", "on", "off"], default="auto")
    p_gsplat_ladder.add_argument("--depth-supervision", choices=["auto", "off", "required"], default="auto")
    p_gsplat_ladder.add_argument("--normal-supervision", choices=["auto", "off", "required"], default="auto")
    p_gsplat_ladder.add_argument("--mcmc-refine-every", default="auto", metavar="auto|N")
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
    p_ladder.add_argument("--masks", choices=["auto", "off", "required"], default="auto")
    p_ladder.add_argument("--normalization", choices=["auto", "on", "off"], default="auto")
    p_ladder.add_argument("--depth-supervision", choices=["auto", "off", "required"], default="auto")
    p_ladder.add_argument("--normal-supervision", choices=["auto", "off", "required"], default="auto")
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
    elif args.command == "import-360":
        payload = import_equirectangular(
            args.input,
            args.out,
            size=args.size,
            fov_degrees=args.fov,
            target_panoramas=args.target_panoramas,
        )
    elif args.command == "sfm-360-rig":
        payload = run_equirectangular_rig_sfm(
            args.package,
            args.out,
            method=args.method,
            overlap=args.overlap,
            max_features=args.max_features,
            allow_cpu_matching=args.allow_cpu_matching,
            dry_run=args.dry_run,
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
            frame_exclusions=args.frame_exclusions,
        )
    elif args.command == "prepare-training-supervision":
        payload = prepare_training_supervision(
            args.package,
            confidence_minimum=args.confidence_minimum,
            derive_normals=not args.no_derive_normals,
        )
    elif args.command == "remove-background":
        payload = remove_background(
            args.images,
            args.out,
            mask_dir=args.mask_dir,
            mode=args.mode,
            threshold=args.threshold,
            model_mode=args.model_mode,
            dry_run=args.dry_run,
        )
    elif args.command == "build-rgbd-seed":
        payload = build_rgbd_metric_seed(
            args.capture,
            args.package,
            args.out,
            minimum_cameras=args.min_cameras,
            max_median_fraction=args.max_median_fraction,
            max_p95_fraction=args.max_p95_fraction,
            confidence_minimum=args.confidence_minimum,
            voxel_size=args.voxel_size,
            max_points=args.max_points,
            seed_source=args.seed_source,
        )
    elif args.command == "build-rgbd-tsdf":
        payload = build_rgbd_tsdf(args.handoff, args.out)
    elif args.command == "build-hybrid-surface":
        payload = build_hybrid_surface(
            args.handoff,
            args.tsdf_report,
            args.out,
            maximum_distance=args.maximum_distance,
            minimum_normal_dot=args.minimum_normal_dot,
            ambiguity_epsilon=args.ambiguity_epsilon,
            collider_triangle_budget=args.collider_triangle_budget,
        )
    elif args.command == "reconstruct":
        payload = reconstruct_capture(
            args.capture,
            args.out,
            backend=args.backend,
            backend_root=args.backend_root,
            recipe=args.recipe,
            steps=parse_steps(args.steps),
            qa_render_dir=args.qa_render_dir,
            qa_pairs_json=args.qa_pairs_json,
            qa_provenance_json=args.qa_provenance_json,
            resume=args.resume,
            dry_run=args.dry_run,
            stop_after=args.stop_after,
            allow_cpu_matching=args.allow_cpu_matching,
            retrieval_top_k=args.retrieval_top_k,
            prune_alpha=args.prune_alpha,
            max_pruned_fraction=args.max_pruned_fraction,
            stop_reset_at=args.stop_reset_at,
            normalization=args.normalization,
            mcmc_refine_every=args.mcmc_refine_every,
            seed_source=args.seed_source,
            depth_supervision=args.depth_supervision,
            normal_supervision=args.normal_supervision,
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
            navigation_mesh=args.navigation_mesh,
            mesh_report=args.mesh_report,
            room_semantics=args.room_semantics,
            camera_trajectory=args.camera_trajectory,
            planes=args.planes,
            metric_scale_report=args.metric_scale_report,
            known_scale_report=args.known_scale_report,
            collision_candidate=args.collision_candidate,
            collision_report=args.collision_report,
            render_source_qa=args.render_source_qa,
            measurement_points=args.measurement_points,
            measurement_points_frame=args.measurement_points_frame,
            image_dir_name=args.image_dir,
            sparse_dir_name=args.sparse_dir,
            copy_files=args.copy_files,
            capture_profile=args.capture_profile,
        )
    elif args.command == "replay-live-session":
        payload = replay_live_session(
            args.capture,
            receiver=args.receiver,
            session_id=args.session_id,
            delay_ms=args.delay_ms,
            shuffle=args.shuffle,
            seed=args.seed,
            duplicate_every=args.duplicate_every,
            disconnect_after=args.disconnect_after,
            disconnect_seconds=args.disconnect_seconds,
            resume=args.resume,
        )
    elif args.command == "train-vksplat":
        payload = run_vksplat(args.package, args.out, args.vksplat_root, steps=args.steps, dry_run=args.dry_run, save_train_renders=args.save_train_renders, stop_reset_at=args.stop_reset_at, masks=args.masks, normalization=args.normalization, depth_supervision=args.depth_supervision, normal_supervision=args.normal_supervision)
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
            normalization=args.normalization,
        )
    elif args.command == "train-gsplat":
        photometric = "none" if args.no_bilateral_grid else args.photometric
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
            random_bkgd=not args.no_random_bkgd,
            max_gaussians=args.max_gaussians,
            photometric=photometric,
            masks=args.masks,
            normalization=args.normalization,
            mcmc_refine_every=args.mcmc_refine_every,
            depth_supervision=args.depth_supervision,
            normal_supervision=args.normal_supervision,
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
    elif args.command == "export-spz":
        payload = export_spz(
            args.input,
            args.out,
            converter=args.converter,
            viewer_evidence=args.viewer_evidence,
            sample_limit=args.sample_limit,
            max_position_p95_fraction=args.max_position_p95_fraction,
            max_color_mae=args.max_color_mae,
            dry_run=args.dry_run,
        )
    elif args.command == "build-collision-candidate":
        payload = build_collision_candidate(
            args.mesh,
            args.mesh_report,
            args.out,
            max_faces=args.max_faces,
            cell_size=args.cell_size,
            intent=args.intent,
        )
    elif args.command == "validate-apriltag-scale":
        payload = validate_apriltag_scale(
            args.package,
            args.out,
            tag_size_meters=args.tag_size_meters,
            detections_json=args.detections_json,
            artifact=args.artifact,
            family=args.family,
            min_views=args.min_views,
            max_reprojection_p95=args.max_reprojection_p95,
            max_edge_cv=args.max_edge_cv,
            max_scale_error_fraction=args.max_scale_error_fraction,
            image_dir_name=args.image_dir,
            sparse_dir_name=args.sparse_dir,
        )
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
            camera_policy=args.camera_policy,
            view_graph_calibration=args.view_graph_calibration,
            masks=args.masks,
            post_ba_backend=args.post_ba_backend,
            capture_manifest=args.capture_manifest,
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
        payload = prune_ply_by_alpha(
            args.input,
            args.out,
            min_alpha=args.min_alpha,
            max_radius=args.max_radius,
            max_dropped_fraction=args.max_dropped_fraction,
        )
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
            photometric=args.photometric,
            masks=args.masks,
            normalization=args.normalization,
            mcmc_refine_every=args.mcmc_refine_every,
            depth_supervision=args.depth_supervision,
            normal_supervision=args.normal_supervision,
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
            masks=args.masks,
            normalization=args.normalization,
            depth_supervision=args.depth_supervision,
            normal_supervision=args.normal_supervision,
            max_psnr_drop=args.max_psnr_drop,
            max_ssim_drop=args.max_ssim_drop,
            max_mae_increase=args.max_mae_increase,
            max_correlation_drop=args.max_correlation_drop,
        )
    elif args.command == "doctor":
        payload = {
            "schema": "capture_splat.doctor.v0.5",
            "tools": {
                name: shutil.which(name)
                for name in ("colmap", "glomap", "ffmpeg", "ffprobe", "splat-transform")
            },
            "colmap_cuda": colmap_has_cuda(),
            "colmap_capabilities": colmap_capabilities(),
            "hloc": hloc_status(),
            "apriltag": apriltag_status(),
            "vksplat": vksplat_doctor(args.vksplat_root),
            "gsplat": gsplat_doctor(args.gsplat_root),
            "three_dgs_cpp": _external_source_status(args.three_dgs_cpp_root, ["CMakeLists.txt"]),
            "andrew_3dgs": _external_source_status(args.andrew_3dgs_root, ["CMakeLists.txt"]),
        }
    else:
        raise AssertionError(args.command)
    print(json.dumps(payload, indent=2, allow_nan=False))
    if args.command == "replay-live-session" and payload["status"] == "interrupted":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
