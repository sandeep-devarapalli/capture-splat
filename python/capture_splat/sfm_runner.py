from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .background_sphere import append_background_sphere
from .json_utils import write_json_strict

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
) -> list[list[str]]:
    database = out_dir / "database.db"
    sparse = out_dir / "sparse"
    commands: list[list[str]] = [[
        "colmap", "feature_extractor",
        "--database_path", str(database),
        "--image_path", str(images_dir),
        "--ImageReader.single_camera", "1",
        "--SiftExtraction.max_num_features", str(int(max_features)),
    ]]
    if matcher == "sequential":
        match_command = [
            "colmap", "sequential_matcher",
            "--database_path", str(database),
            "--SequentialMatching.overlap", str(int(overlap)),
            "--SequentialMatching.loop_detection", "1" if loop_detection else "0",
        ]
        if loop_detection and vocab_tree is not None:
            match_command += ["--SequentialMatching.vocab_tree_path", str(vocab_tree)]
        commands.append(match_command)
    else:
        commands.append(["colmap", "exhaustive_matcher", "--database_path", str(database)])
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
        commands.append([
            "colmap", "mapper",
            "--database_path", str(database),
            "--image_path", str(images_dir),
            "--output_path", str(sparse),
        ])
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


def align_orientation(images_dir: Path, sparse_zero: Path, dry_run: bool = False) -> dict[str, Any]:
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
    method: str = "colmap",
    matcher: str = "sequential",
    overlap: int = 30,
    loop_detection: bool = True,
    vocab_tree: Path | None = None,
    max_features: int = 8192,
    min_reject_ratio: float = 0.60,
    min_hold_ratio: float = 0.85,
    copy_images: bool = True,
    background_sphere: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    images_dir = images_dir.resolve()
    out_dir = out_dir.resolve()
    if not images_dir.is_dir():
        raise FileNotFoundError(f"images directory missing: {images_dir}")
    if matcher == "retrieval":
        raise RuntimeError("retrieval matching requires hloc (see scripts/setup_sfm.sh); use sequential or exhaustive")
    blockers: list[str] = []
    if find_binary("colmap") is None:
        blockers.append("colmap_binary_missing")
    if method == "glomap" and find_binary("glomap") is None:
        blockers.append("glomap_binary_missing")
    if loop_detection and matcher == "sequential" and vocab_tree is None:
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
    total_images = count_images(run_images)
    commands = build_commands(run_images, out_dir, method, matcher, overlap, loop_detection, vocab_tree, max_features)
    summary: dict[str, Any] = {
        "schema": SUMMARY_SCHEMA,
        "images_dir": str(run_images),
        "output_dir": str(out_dir),
        "method": method,
        "matcher": matcher,
        "overlap": overlap,
        "loop_detection": loop_detection,
        "vocab_tree": str(vocab_tree) if vocab_tree else None,
        "max_features": max_features,
        "total_images": total_images,
        "commands": commands,
        "blockers": blockers,
        "dry_run": dry_run,
        "authority": {"registration_evidence": True, "quality_claim": False},
    }
    if blockers or dry_run:
        summary["decision"] = "blocked" if blockers else "dry_run"
        write_json_strict(out_dir / "capture_splat_sfm_summary.json", summary)
        if blockers:
            raise RuntimeError(f"sfm blocked: {', '.join(blockers)}")
        return summary
    (out_dir / "sparse").mkdir(parents=True, exist_ok=True)
    for command in commands:
        completed = subprocess.run(command, text=True)
        if completed.returncode != 0:
            summary["decision"] = "reject"
            summary["failed_command"] = command
            write_json_strict(out_dir / "capture_splat_sfm_summary.json", summary)
            raise RuntimeError(f"sfm step failed ({command[0]} {command[1]}), exit {completed.returncode}")
    sparse_zero = select_best_sparse_subdir(out_dir / "sparse")
    if sparse_zero is None:
        summary["decision"] = "reject"
        summary["error"] = "no reconstruction produced"
        write_json_strict(out_dir / "capture_splat_sfm_summary.json", summary)
        raise RuntimeError("sfm produced no reconstruction")
    summary["orientation_alignment"] = align_orientation(run_images, sparse_zero)
    model_to_text(sparse_zero)
    stats = read_model_stats(sparse_zero)
    summary["model"] = stats
    decision, ratio = decide(stats["registered_images"], total_images, min_reject_ratio, min_hold_ratio)
    summary["registered_ratio"] = ratio
    summary["decision"] = decision
    summary["sparse_dir"] = str(sparse_zero)
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
        "blockers": [] if find_binary("colmap") else ["colmap_binary_missing"],
        "dry_run": dry_run,
        "authority": {"registration_evidence": True, "pose_prior": "device_poses", "quality_claim": False},
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    if summary["blockers"] or dry_run:
        summary["decision"] = "blocked" if summary["blockers"] else "dry_run"
        write_json_strict(out_dir / "capture_splat_sfm_summary.json", summary)
        if summary["blockers"]:
            raise RuntimeError("triangulate blocked: colmap_binary_missing")
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
    summary["orientation_alignment"] = align_orientation(images_dir, sparse_zero)
    model_to_text(sparse_zero)
    stats = read_model_stats(sparse_zero)
    summary["model"] = stats
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
    parser.add_argument("--method", choices=["colmap", "glomap"], default="colmap")
    parser.add_argument("--matcher", choices=["sequential", "exhaustive", "retrieval"], default="sequential")
    parser.add_argument("--overlap", type=int, default=30)
    parser.add_argument("--no-loop-detection", action="store_true")
    parser.add_argument("--vocab-tree", type=Path)
    parser.add_argument("--max-features", type=int, default=8192)
    parser.add_argument("--no-copy-images", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    summary = run_sfm(
        args.images,
        args.out,
        method=args.method,
        matcher=args.matcher,
        overlap=args.overlap,
        loop_detection=not args.no_loop_detection,
        vocab_tree=args.vocab_tree,
        max_features=args.max_features,
        copy_images=not args.no_copy_images,
        dry_run=args.dry_run,
    )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
