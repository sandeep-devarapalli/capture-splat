from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from .background_sphere import append_background_sphere
from .hloc_runner import hloc_status, planned_frontend, run_hloc_frontend
from .json_utils import load_json_strict, write_json_strict
from .training_supervision import copy_capture_supervision_assets
from .scene_transform import PACKAGE_ORIENTATION_NAME, write_package_orientation_transform
from .sfm_evidence import (
    apply_camera_priors,
    camera_evidence_report,
    copy_valid_masks,
    discover_capture_manifest,
    external_camera_options,
    load_frame_evidence,
    write_fixed_evaluation_set,
)

SUMMARY_SCHEMA = "capture_splat.sfm_summary.v0.1"
GLOMAP_MAPPER_ARGS = [
    "--ba_iteration_num", "5",
    "--skip_pruning", "0",
    "--GlobalPositioning.max_num_iterations", "300",
    "--BundleAdjustment.max_num_iterations", "500",
    "--Thresholds.max_epipolar_error_E=0.5",
    "--Thresholds.max_epipolar_error_F=1.5",
    "--Thresholds.max_epipolar_error_H=1.5",
    "--Thresholds.min_inlier_num=50",
    "--Thresholds.min_inlier_ratio=0.4",
    "--Thresholds.max_rotation_error=5",
]


def find_binary(name: str) -> str | None:
    return shutil.which(name)


def colmap_has_cuda() -> bool | None:
    if find_binary("colmap") is None:
        return None
    try:
        completed = subprocess.run(["colmap", "help"], text=True, capture_output=True)
    except OSError:
        return None
    banner = (completed.stdout or "") + (completed.stderr or "")
    if "with CUDA" in banner:
        return True
    if "without CUDA" in banner:
        return False
    return None


def colmap_capabilities() -> dict[str, Any]:
    capabilities: dict[str, Any] = {
        "global_mapper": False,
        "view_graph_calibrator": False,
        "rig_configurator": False,
        "caspar": False,
    }
    if find_binary("colmap") is None:
        return capabilities
    try:
        help_result = subprocess.run(["colmap", "help"], text=True, capture_output=True)
        bundle_result = subprocess.run(["colmap", "bundle_adjuster", "-h"], text=True, capture_output=True)
    except OSError:
        return capabilities
    commands = (help_result.stdout or "") + (help_result.stderr or "")
    bundle = (bundle_result.stdout or "") + (bundle_result.stderr or "")
    capabilities.update({
        "global_mapper": "global_mapper" in commands,
        "view_graph_calibrator": "view_graph_calibrator" in commands,
        "rig_configurator": "rig_configurator" in commands,
        "caspar": "BundleAdjustmentCaspar.gpu_index" in bundle,
    })
    return capabilities


def normalize_method(method: str) -> str:
    return "incremental" if method == "colmap" else method


def resolve_camera_policy(requested: str, camera_report: dict[str, Any], prepared_capture: bool = False) -> str:
    if requested not in {"auto", "per-frame", "single"}:
        raise ValueError(f"unsupported camera policy: {requested}")
    if requested == "auto":
        return "per-frame" if prepared_capture and camera_report.get("complete") else "single"
    if requested == "per-frame" and not camera_report.get("complete"):
        raise ValueError("per-frame camera policy requires complete finite intrinsics")
    return requested


def resolve_view_graph_calibration(requested: str, method: str, camera_report: dict[str, Any]) -> bool:
    if requested not in {"auto", "on", "off"}:
        raise ValueError(f"unsupported view graph calibration policy: {requested}")
    if method != "global":
        return False
    if requested == "auto":
        return not bool(camera_report.get("complete"))
    return requested == "on"


def count_images(images_dir: Path) -> int:
    return sum(1 for path in images_dir.iterdir() if path.suffix.lower() in {".jpg", ".jpeg", ".png"})


def build_commands(
    images_dir: Path,
    out_dir: Path,
    method: str,
    matcher: str,
    overlap: int,
    loop_detection: bool,
    vocab_tree: Path | None,
    max_features: int,
    camera_policy: str = "single",
    view_graph_calibration: bool = False,
    mask_dir: Path | None = None,
    single_camera_options: tuple[str, str] | None = None,
    use_gpu: bool = True,
) -> list[list[str]]:
    method = normalize_method(method)
    database = out_dir / "database.db"
    mapping_database = out_dir / "database_global.db" if view_graph_calibration else database
    sparse = out_dir / "sparse"
    commands: list[list[str]] = []
    if matcher == "retrieval":
        commands.extend(planned_frontend(images_dir, out_dir, database, 32))
    else:
        image_reader = ["--ImageReader.single_camera_per_image", "1"] if camera_policy == "per-frame" else ["--ImageReader.single_camera", "1"]
        if camera_policy == "single" and single_camera_options is not None:
            image_reader += [
                "--ImageReader.camera_model", single_camera_options[0],
                "--ImageReader.camera_params", single_camera_options[1],
            ]
        commands.append([
            "colmap", "feature_extractor",
            "--database_path", str(database),
            "--image_path", str(images_dir),
            *image_reader,
            *( ["--ImageReader.mask_path", str(mask_dir)] if mask_dir is not None else [] ),
            "--FeatureExtraction.use_gpu", "1" if use_gpu else "0",
            "--SiftExtraction.max_num_features", str(int(max_features)),
        ])
    if matcher == "sequential":
        match_command = [
            "colmap", "sequential_matcher",
            "--database_path", str(database),
            "--SequentialMatching.overlap", str(int(overlap)),
            "--SequentialMatching.loop_detection", "1" if loop_detection else "0",
            "--FeatureMatching.use_gpu", "1" if use_gpu else "0",
        ]
        if loop_detection and vocab_tree is not None:
            match_command += ["--SequentialMatching.vocab_tree_path", str(vocab_tree)]
        commands.append(match_command)
    elif matcher == "exhaustive":
        commands.append([
            "colmap", "exhaustive_matcher",
            "--database_path", str(database),
            "--FeatureMatching.use_gpu", "1" if use_gpu else "0",
        ])
    elif matcher != "retrieval":
        raise ValueError(f"unsupported matcher: {matcher}")
    if view_graph_calibration:
        commands.append([
            "colmap", "view_graph_calibrator",
            "--database_path", str(mapping_database),
        ])
    if method == "glomap":
        commands.append([
            "glomap", "mapper",
            "--database_path", str(database),
            "--image_path", str(images_dir),
            "--output_path", str(sparse),
            *GLOMAP_MAPPER_ARGS,
        ])
        commands.append([
            "colmap", "image_registrator",
            "--database_path", str(database),
            "--input_path", str(sparse / "0"),
            "--output_path", str(sparse / "0"),
        ])
    else:
        mapper_command = [
            "colmap", "global_mapper" if method == "global" else "mapper",
            "--database_path", str(mapping_database),
            "--image_path", str(images_dir),
            "--output_path", str(sparse),
        ]
        if not use_gpu and method == "global":
            mapper_command += [
                "--GlobalMapper.gp_use_gpu", "0",
                "--GlobalMapper.ba_ceres_use_gpu", "0",
            ]
        elif not use_gpu:
            mapper_command += ["--Mapper.ba_use_gpu", "0"]
        commands.append(mapper_command)
    return commands


def select_best_sparse_subdir(sparse_dir: Path) -> Path | None:
    candidates = []
    for subdir in sorted(path for path in sparse_dir.iterdir() if path.is_dir()):
        for name in ("points3D.bin", "points3D.txt"):
            points = subdir / name
            if points.exists():
                candidates.append((points.stat().st_size, subdir))
                break
    if not candidates:
        return None
    best = max(candidates, key=lambda entry: entry[0])[1]
    zero = sparse_dir / "0"
    if best != zero:
        old = sparse_dir / "old_0"
        if zero.exists():
            if old.exists():
                shutil.rmtree(old)
            shutil.move(str(zero), str(old))
        shutil.copytree(best, zero)
    return zero


def align_orientation(
    images_dir: Path,
    sparse_zero: Path,
    dry_run: bool = False,
    report_path: Path | None = None,
) -> dict[str, Any]:
    aligned = sparse_zero.parent / "0_aligned"
    backup = sparse_zero.parent / "0_before_alignment"
    command = [
        "colmap", "model_orientation_aligner",
        "--image_path", str(images_dir),
        "--input_path", str(sparse_zero),
        "--output_path", str(aligned),
    ]
    result: dict[str, Any] = {"command": command, "aligned": False}
    if dry_run:
        return result
    aligned.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(command, text=True, capture_output=True)
    result["returncode"] = completed.returncode
    if completed.returncode != 0 or not any(aligned.iterdir()):
        result["error"] = (completed.stderr or "")[-2000:]
        shutil.rmtree(aligned, ignore_errors=True)
        return result
    if backup.exists():
        shutil.rmtree(backup)
    shutil.move(str(sparse_zero), str(backup))
    shutil.move(str(aligned), str(sparse_zero))
    result["aligned"] = True
    result["backup"] = str(backup)
    if report_path is not None:
        try:
            model_to_text(backup)
            model_to_text(sparse_zero)
            result["package_orientation_transform"] = write_package_orientation_transform(
                backup,
                sparse_zero,
                report_path,
            )
        except (OSError, RuntimeError, ValueError) as error:
            result["package_orientation_transform"] = {
                "status": "unavailable",
                "reason": str(error),
            }
    return result


def model_to_text(sparse_zero: Path) -> None:
    if (sparse_zero / "images.txt").exists():
        return
    subprocess.run(
        ["colmap", "model_converter", "--input_path", str(sparse_zero), "--output_path", str(sparse_zero), "--output_type", "TXT"],
        check=True,
        text=True,
        capture_output=True,
    )


def read_model_stats(sparse_zero: Path) -> dict[str, Any]:
    images_txt = sparse_zero / "images.txt"
    points_txt = sparse_zero / "points3D.txt"
    stats: dict[str, Any] = {"registered_images": 0, "points": 0, "observations": 0, "mean_track_length": None, "mean_reprojection_error": None}
    if images_txt.exists():
        registered = 0
        for line in images_txt.read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) < 10 or line.startswith("#"):
                continue
            try:
                float(parts[9])
            except ValueError:
                registered += 1
        stats["registered_images"] = registered
    if points_txt.exists():
        errors: list[float] = []
        track_total = 0
        point_count = 0
        with points_txt.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip() or line.startswith("#"):
                    continue
                parts = line.split()
                if len(parts) < 8:
                    continue
                point_count += 1
                errors.append(float(parts[7]))
                track_total += (len(parts) - 8) // 2
        stats["points"] = point_count
        stats["observations"] = track_total
        if point_count:
            stats["mean_track_length"] = track_total / point_count
            stats["mean_reprojection_error"] = sum(errors) / len(errors)
    return stats


def read_camera_models(cameras_txt: Path) -> list[str]:
    models: set[str] = set()
    if not cameras_txt.exists():
        return []
    for line in cameras_txt.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if line.startswith("#") or len(parts) < 5:
            continue
        try:
            int(parts[0])
        except ValueError:
            continue
        models.add(parts[1])
    return sorted(models)


def run_post_bundle_adjustment(sparse_zero: Path, backend: str) -> dict[str, Any]:
    if backend == "none":
        return {"backend": "none", "status": "not_requested"}
    model_to_text(sparse_zero)
    before = read_model_stats(sparse_zero)
    camera_models = read_camera_models(sparse_zero / "cameras.txt")
    if backend == "caspar" and any(model not in {"PINHOLE", "SIMPLE_RADIAL"} for model in camera_models):
        raise RuntimeError(f"Caspar does not support camera models: {', '.join(camera_models)}")
    output = sparse_zero.parent / "0_post_ba"
    backup = sparse_zero.parent / "0_before_post_ba"
    shutil.rmtree(output, ignore_errors=True)
    output.mkdir(parents=True)
    command = [
        "colmap", "bundle_adjuster",
        "--input_path", str(sparse_zero),
        "--output_path", str(output),
    ]
    if backend == "caspar":
        command += ["--BundleAdjustment.backend", "CASPAR"]
    started = time.monotonic()
    completed = subprocess.run(command, text=True, capture_output=True)
    result = {
        "backend": backend,
        "command": command,
        "returncode": completed.returncode,
        "camera_models": camera_models,
        "pose_priors_used": False,
        "runtime_seconds": time.monotonic() - started,
        "model_before": before,
    }
    if completed.returncode != 0 or not any(output.iterdir()):
        result["error"] = ((completed.stderr or "") + (completed.stdout or ""))[-2000:]
        shutil.rmtree(output, ignore_errors=True)
        raise RuntimeError(f"{backend} post bundle adjustment failed")
    shutil.rmtree(backup, ignore_errors=True)
    shutil.move(str(sparse_zero), str(backup))
    shutil.move(str(output), str(sparse_zero))
    model_to_text(sparse_zero)
    after = read_model_stats(sparse_zero)
    result.update({"status": "completed", "backup": str(backup), "model_after": after})
    reprojection = after.get("mean_reprojection_error")
    if after["registered_images"] != before["registered_images"] or (
        reprojection is not None and not math.isfinite(float(reprojection))
    ):
        shutil.rmtree(sparse_zero, ignore_errors=True)
        shutil.move(str(backup), str(sparse_zero))
        raise RuntimeError(f"{backend} post bundle adjustment failed model-preservation gates")
    return result


def decide(registered: int, total: int, min_reject_ratio: float, min_hold_ratio: float) -> tuple[str, float]:
    ratio = registered / total if total else 0.0
    if ratio < min_reject_ratio:
        return "reject", ratio
    if ratio < min_hold_ratio:
        return "hold", ratio
    return "promote", ratio


def run_sfm(
    images_dir: Path,
    out_dir: Path,
    method: str = "global",
    matcher: str = "exhaustive",
    features: str = "sift",
    retrieval_top_k: int = 32,
    overlap: int = 30,
    loop_detection: bool = True,
    vocab_tree: Path | None = None,
    max_features: int = 8192,
    min_reject_ratio: float = 0.60,
    min_hold_ratio: float = 0.85,
    copy_images: bool = True,
    background_sphere: bool = False,
    allow_cpu_matching: bool = False,
    dry_run: bool = False,
    camera_policy: str = "auto",
    view_graph_calibration: str = "auto",
    masks: str = "auto",
    post_ba_backend: str = "none",
    capture_manifest: Path | None = None,
) -> dict[str, Any]:
    images_dir = images_dir.resolve()
    out_dir = out_dir.resolve()
    if not images_dir.is_dir():
        raise FileNotFoundError(f"images directory missing: {images_dir}")
    if features not in {"sift", "hloc"}:
        raise ValueError(f"unsupported features: {features}")
    if matcher == "retrieval" and features != "hloc":
        raise ValueError("retrieval matcher requires --features hloc")
    if features == "hloc" and matcher != "retrieval":
        raise ValueError("HLOC features require --matcher retrieval")
    requested_method = method
    method = normalize_method(method)
    if method not in {"global", "incremental", "glomap"}:
        raise ValueError(f"unsupported SfM method: {requested_method}")
    if masks not in {"auto", "off", "required"}:
        raise ValueError(f"unsupported mask policy: {masks}")
    if post_ba_backend not in {"none", "ceres", "caspar"}:
        raise ValueError(f"unsupported post-BA backend: {post_ba_backend}")
    retrieval_top_k = max(1, int(retrieval_top_k))
    blockers: list[str] = []
    hloc = hloc_status()
    capabilities = colmap_capabilities()
    capture_manifest = capture_manifest.resolve() if capture_manifest is not None else discover_capture_manifest(images_dir)
    capture_metadata = load_json_strict(capture_manifest) if capture_manifest is not None else {}
    prepared_capture = capture_metadata.get("source") == "capture_splat.prepare_capture"
    frame_evidence = load_frame_evidence(capture_manifest)
    camera_report = camera_evidence_report(images_dir, frame_evidence)
    warnings: list[str] = []
    try:
        resolved_camera_policy = resolve_camera_policy(camera_policy, camera_report, prepared_capture)
    except ValueError as error:
        resolved_camera_policy = camera_policy
        blockers.append(str(error))
    try:
        resolved_view_graph_calibration = resolve_view_graph_calibration(
            view_graph_calibration, method, camera_report if resolved_camera_policy == "per-frame" else {}
        )
    except ValueError as error:
        resolved_view_graph_calibration = False
        blockers.append(str(error))
    source_mask_dir = images_dir.parent / "masks" / "valid"
    source_masks = sorted(source_mask_dir.glob("*.png")) if source_mask_dir.is_dir() else []
    image_names = sorted(path.name for path in images_dir.iterdir() if path.suffix.lower() in {".jpg", ".jpeg", ".png"})
    missing_masks = [name for name in image_names if not (source_mask_dir / f"{name}.png").exists()]
    resolved_mask_dir = source_mask_dir if masks != "off" and source_masks and not missing_masks else None
    if masks == "required" and (resolved_mask_dir is None or missing_masks):
        blockers.append("required_valid_masks_missing")
    if masks == "auto" and source_masks and missing_masks:
        warnings.append("incomplete_valid_masks_disabled")
    if requested_method == "colmap":
        warnings.append("method_colmap_is_deprecated_incremental_alias")
    if camera_policy == "auto" and camera_report.get("complete") and not prepared_capture:
        warnings.append("generic_images_single_camera_fallback")
    single_camera_options = (
        external_camera_options(images_dir, frame_evidence)
        if resolved_camera_policy == "single" and not prepared_capture
        else None
    )
    if features == "hloc" and single_camera_options is not None:
        blockers.append("hloc_external_distortion_preservation_unavailable")
    colmap_cuda: bool | None = None
    if find_binary("colmap") is None:
        blockers.append("colmap_binary_missing")
    else:
        colmap_cuda = colmap_has_cuda()
        if colmap_cuda is not True and not allow_cpu_matching:
            blockers.append("colmap_cuda_missing")
    if method == "glomap" and find_binary("glomap") is None:
        blockers.append("glomap_binary_missing")
    if method == "global" and not capabilities["global_mapper"]:
        blockers.append("colmap_global_mapper_missing")
    if resolved_view_graph_calibration and not capabilities["view_graph_calibrator"]:
        blockers.append("colmap_view_graph_calibrator_missing")
    if post_ba_backend != "none" and method != "global":
        blockers.append("post_ba_requires_global_mapper")
    if post_ba_backend == "caspar" and not capabilities["caspar"]:
        blockers.append("colmap_caspar_missing")
    if matcher == "retrieval" and not hloc["ready"]:
        blockers.append("hloc_missing")
    if matcher != "sequential":
        loop_detection = False
    elif loop_detection and vocab_tree is None:
        loop_detection = False
    out_dir.mkdir(parents=True, exist_ok=True)
    package_images = out_dir / "images"
    if copy_images and package_images.resolve() != images_dir:
        package_images.mkdir(parents=True, exist_ok=True)
        for path in sorted(images_dir.iterdir()):
            if path.suffix.lower() in {".jpg", ".jpeg", ".png"}:
                target = package_images / path.name
                if not target.exists():
                    shutil.copy2(path, target)
        run_images = package_images
    else:
        run_images = images_dir
    package_capture_manifest = capture_manifest
    supervision_copy = {"copied": 0, "paths": [], "missing": [], "complete": True}
    if capture_manifest is not None and copy_images:
        target_manifest = out_dir / "capture.json"
        if target_manifest.resolve() != capture_manifest:
            shutil.copy2(capture_manifest, target_manifest)
        package_capture_manifest = target_manifest
        supervision_copy = copy_capture_supervision_assets(
            capture_manifest.parent,
            out_dir,
            capture_metadata,
        )
        if supervision_copy["missing"]:
            warnings.append("capture_supervision_sidecars_missing")
    if resolved_mask_dir is not None and copy_images:
        mask_copy = copy_valid_masks(resolved_mask_dir, out_dir / "masks" / "valid")
        run_mask_dir = out_dir / "masks" / "valid"
    else:
        mask_copy = {"status": "disabled" if masks == "off" else "source", "copied": 0}
        run_mask_dir = resolved_mask_dir
    total_images = count_images(run_images)
    commands = build_commands(
        run_images,
        out_dir,
        method,
        matcher,
        overlap,
        loop_detection,
        vocab_tree,
        max_features,
        camera_policy=resolved_camera_policy,
        view_graph_calibration=resolved_view_graph_calibration,
        mask_dir=run_mask_dir,
        single_camera_options=single_camera_options,
        use_gpu=colmap_cuda is True,
    )
    if matcher == "retrieval" and retrieval_top_k != 32:
        commands[:5] = planned_frontend(run_images, out_dir, out_dir / "database.db", retrieval_top_k)
    summary: dict[str, Any] = {
        "schema": SUMMARY_SCHEMA,
        "images_dir": str(run_images),
        "output_dir": str(out_dir),
        "requested_method": requested_method,
        "method": method,
        "matcher": matcher,
        "features": features,
        "retrieval_top_k": retrieval_top_k if matcher == "retrieval" else None,
        "hloc": hloc,
        "colmap_capabilities": capabilities,
        "capture_manifest": str(capture_manifest) if capture_manifest else None,
        "package_capture_manifest": str(package_capture_manifest) if package_capture_manifest else None,
        "supervision_copy": supervision_copy,
        "camera_policy": {"requested": camera_policy, "resolved": resolved_camera_policy},
        "camera_evidence": camera_report,
        "prepared_capture": prepared_capture,
        "single_camera_options": {
            "model": single_camera_options[0],
            "params": single_camera_options[1],
            "distortion_preserved": True,
        } if single_camera_options is not None else None,
        "view_graph_calibration": {
            "requested": view_graph_calibration,
            "resolved": resolved_view_graph_calibration,
            "database_copy": str(out_dir / "database_global.db") if resolved_view_graph_calibration else None,
        },
        "masks": {
            "requested": masks,
            "source": str(source_mask_dir) if source_mask_dir.is_dir() else None,
            "resolved": str(run_mask_dir) if run_mask_dir else None,
            "available": len(source_masks),
            "missing": missing_masks,
            "copy": mask_copy,
            "semantics": "white_valid_for_features_and_training",
        },
        "post_ba_backend": post_ba_backend,
        "overlap": overlap,
        "loop_detection": loop_detection,
        "vocab_tree": str(vocab_tree) if vocab_tree else None,
        "max_features": max_features,
        "total_images": total_images,
        "commands": commands,
        "blockers": blockers,
        "warnings": warnings,
        "colmap_cuda": colmap_cuda,
        "cpu_matching_override": bool(allow_cpu_matching and colmap_cuda is not True),
        "dry_run": dry_run,
        "authority": {"registration_evidence": False, "quality_claim": False},
    }
    write_json_strict(out_dir / "metadata" / "camera_evidence.json", camera_report)
    if blockers or dry_run:
        summary["decision"] = "blocked" if blockers else "dry_run"
        write_json_strict(out_dir / "capture_splat_sfm_summary.json", summary)
        if blockers:
            raise RuntimeError(f"sfm blocked: {', '.join(blockers)}")
        return summary
    (out_dir / "sparse").mkdir(parents=True, exist_ok=True)
    mapping_commands = commands
    if matcher == "retrieval":
        try:
            summary["hloc_frontend"] = run_hloc_frontend(
                run_images,
                out_dir,
                out_dir / "database.db",
                top_k=retrieval_top_k,
                camera_policy=resolved_camera_policy,
                capture_manifest=capture_manifest,
                mask_dir=run_mask_dir,
                masks=masks,
            )
        except Exception as error:
            summary["decision"] = "reject"
            summary["failed_stage"] = "hloc_frontend"
            summary["error"] = str(error)
            write_json_strict(out_dir / "capture_splat_sfm_summary.json", summary)
            raise RuntimeError(f"sfm HLOC frontend failed: {error}") from error
        mapping_commands = commands[5:]
    for command in mapping_commands:
        if command[1] == "view_graph_calibrator":
            shutil.copy2(out_dir / "database.db", out_dir / "database_global.db")
        completed = subprocess.run(command, text=True)
        if completed.returncode != 0:
            summary["decision"] = "reject"
            summary["failed_command"] = command
            write_json_strict(out_dir / "capture_splat_sfm_summary.json", summary)
            raise RuntimeError(f"sfm step failed ({command[0]} {command[1]}), exit {completed.returncode}")
        if command[1] == "feature_extractor" and resolved_camera_policy == "per-frame":
            try:
                summary["camera_database_update"] = apply_camera_priors(
                    out_dir / "database.db", run_images, frame_evidence
                )
            except Exception as error:
                summary["decision"] = "reject"
                summary["failed_stage"] = "camera_priors"
                summary["error"] = str(error)
                write_json_strict(out_dir / "capture_splat_sfm_summary.json", summary)
                raise RuntimeError(f"sfm camera-prior import failed: {error}") from error
    sparse_zero = select_best_sparse_subdir(out_dir / "sparse")
    if sparse_zero is None:
        summary["decision"] = "reject"
        summary["error"] = "no reconstruction produced"
        write_json_strict(out_dir / "capture_splat_sfm_summary.json", summary)
        raise RuntimeError("sfm produced no reconstruction")
    if post_ba_backend != "none":
        try:
            summary["post_bundle_adjustment"] = run_post_bundle_adjustment(sparse_zero, post_ba_backend)
        except Exception as error:
            summary["decision"] = "reject"
            summary["failed_stage"] = "post_bundle_adjustment"
            summary["error"] = str(error)
            write_json_strict(out_dir / "capture_splat_sfm_summary.json", summary)
            raise
    summary["orientation_alignment"] = align_orientation(
        run_images,
        sparse_zero,
        report_path=out_dir / "metadata" / PACKAGE_ORIENTATION_NAME,
    )
    model_to_text(sparse_zero)
    stats = read_model_stats(sparse_zero)
    summary["model"] = stats
    summary["authority"]["registration_evidence"] = True
    decision, ratio = decide(stats["registered_images"], total_images, min_reject_ratio, min_hold_ratio)
    summary["registered_ratio"] = ratio
    summary["decision"] = decision
    summary["sparse_dir"] = str(sparse_zero)
    summary["fixed_camera_evaluation_set"] = write_fixed_evaluation_set(
        sparse_zero / "images.txt", out_dir / "metadata" / "fixed_camera_evaluation_set.json"
    )
    if background_sphere:
        summary["background_sphere"] = append_background_sphere(sparse_zero)
    write_json_strict(out_dir / "capture_splat_sfm_summary.json", summary)
    return summary


def run_triangulate(
    package_dir: Path,
    out_dir: Path,
    overlap: int = 30,
    loop_detection: bool = False,
    vocab_tree: Path | None = None,
    max_features: int = 8192,
    refine_poses: bool = False,
    min_reject_ratio: float = 0.60,
    min_hold_ratio: float = 0.85,
    background_sphere: bool = False,
    allow_cpu_matching: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    package_dir = package_dir.resolve()
    out_dir = out_dir.resolve()
    images_dir = package_dir / "images"
    sparse_zero = package_dir / "sparse" / "0"
    if not images_dir.is_dir():
        raise FileNotFoundError(f"package images missing: {images_dir}")
    if not (sparse_zero / "images.txt").exists():
        raise FileNotFoundError(f"package poses missing: {sparse_zero / 'images.txt'}")
    if loop_detection and vocab_tree is None:
        loop_detection = False
    database = out_dir / "database.db"
    triangulated = package_dir / "sparse" / "0_triangulated"
    refined = package_dir / "sparse" / "0_refined"
    commands: list[list[str]] = [
        [
            "colmap", "feature_extractor",
            "--database_path", str(database),
            "--image_path", str(images_dir),
            "--ImageReader.single_camera", "1",
            "--SiftExtraction.max_num_features", str(int(max_features)),
        ],
        [
            "colmap", "sequential_matcher",
            "--database_path", str(database),
            "--SequentialMatching.overlap", str(int(overlap)),
            "--SequentialMatching.loop_detection", "1" if loop_detection else "0",
            *(["--SequentialMatching.vocab_tree_path", str(vocab_tree)] if loop_detection and vocab_tree else []),
        ],
        [
            "colmap", "point_triangulator",
            "--database_path", str(database),
            "--image_path", str(images_dir),
            "--input_path", str(sparse_zero),
            "--output_path", str(triangulated),
        ],
    ]
    if refine_poses:
        commands.append([
            "colmap", "bundle_adjuster",
            "--input_path", str(triangulated),
            "--output_path", str(refined),
        ])
    total_images = count_images(images_dir)
    blockers: list[str] = []
    colmap_cuda: bool | None = None
    if find_binary("colmap") is None:
        blockers.append("colmap_binary_missing")
    else:
        colmap_cuda = colmap_has_cuda()
        if colmap_cuda is not True and not allow_cpu_matching:
            blockers.append("colmap_cuda_missing")
    summary: dict[str, Any] = {
        "schema": SUMMARY_SCHEMA,
        "mode": "triangulate_device_pose_prior",
        "package_dir": str(package_dir),
        "output_dir": str(out_dir),
        "overlap": overlap,
        "loop_detection": loop_detection,
        "refine_poses": refine_poses,
        "total_images": total_images,
        "commands": commands,
        "blockers": blockers,
        "colmap_cuda": colmap_cuda,
        "cpu_matching_override": bool(allow_cpu_matching and colmap_cuda is not True),
        "dry_run": dry_run,
        "authority": {"registration_evidence": False, "pose_prior": "device_poses", "quality_claim": False},
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    if summary["blockers"] or dry_run:
        summary["decision"] = "blocked" if summary["blockers"] else "dry_run"
        write_json_strict(out_dir / "capture_splat_sfm_summary.json", summary)
        if summary["blockers"]:
            raise RuntimeError(f"triangulate blocked: {', '.join(blockers)}")
        return summary
    triangulated.mkdir(parents=True, exist_ok=True)
    if refine_poses:
        refined.mkdir(parents=True, exist_ok=True)
    for command in commands:
        completed = subprocess.run(command, text=True)
        if completed.returncode != 0:
            summary["decision"] = "reject"
            summary["failed_command"] = command
            write_json_strict(out_dir / "capture_splat_sfm_summary.json", summary)
            raise RuntimeError(f"triangulate step failed ({command[1]}), exit {completed.returncode}")
    final = refined if refine_poses else triangulated
    backup = package_dir / "sparse" / "0_before_triangulation"
    if backup.exists():
        shutil.rmtree(backup)
    shutil.move(str(sparse_zero), str(backup))
    shutil.move(str(final), str(sparse_zero))
    if triangulated.exists():
        shutil.rmtree(triangulated, ignore_errors=True)
    summary["pose_backup"] = str(backup)
    summary["orientation_alignment"] = align_orientation(
        images_dir,
        sparse_zero,
        report_path=package_dir / "metadata" / PACKAGE_ORIENTATION_NAME,
    )
    model_to_text(sparse_zero)
    stats = read_model_stats(sparse_zero)
    summary["model"] = stats
    summary["authority"]["registration_evidence"] = True
    decision, ratio = decide(stats["registered_images"], total_images, min_reject_ratio, min_hold_ratio)
    summary["registered_ratio"] = ratio
    summary["decision"] = decision
    summary["sparse_dir"] = str(sparse_zero)
    if background_sphere:
        summary["background_sphere"] = append_background_sphere(sparse_zero)
    write_json_strict(out_dir / "capture_splat_sfm_summary.json", summary)
    return summary


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run COLMAP/GLOMAP SfM and produce an orientation-aligned package.")
    parser.add_argument("--images", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--method", choices=["global", "incremental", "glomap", "colmap"], default="global")
    parser.add_argument("--matcher", choices=["sequential", "exhaustive", "retrieval"], default="exhaustive")
    parser.add_argument("--features", choices=["sift", "hloc"], default="sift")
    parser.add_argument("--retrieval-top-k", type=int, default=32)
    parser.add_argument("--overlap", type=int, default=30)
    parser.add_argument("--no-loop-detection", action="store_true")
    parser.add_argument("--vocab-tree", type=Path)
    parser.add_argument("--max-features", type=int, default=8192)
    parser.add_argument("--no-copy-images", action="store_true")
    parser.add_argument("--allow-cpu-matching", action="store_true", help="Run without CUDA COLMAP; recorded in the summary as cpu_matching_override")
    parser.add_argument("--camera-policy", choices=["auto", "per-frame", "single"], default="auto")
    parser.add_argument("--view-graph-calibration", choices=["auto", "on", "off"], default="auto")
    parser.add_argument("--masks", choices=["auto", "off", "required"], default="auto")
    parser.add_argument("--post-ba-backend", choices=["none", "ceres", "caspar"], default="none")
    parser.add_argument("--capture-manifest", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    summary = run_sfm(
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
        allow_cpu_matching=args.allow_cpu_matching,
        dry_run=args.dry_run,
        camera_policy=args.camera_policy,
        view_graph_calibration=args.view_graph_calibration,
        masks=args.masks,
        post_ba_backend=args.post_ba_backend,
        capture_manifest=args.capture_manifest,
    )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
