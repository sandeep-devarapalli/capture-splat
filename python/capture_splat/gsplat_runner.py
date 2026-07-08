from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from .json_utils import write_json_strict
from .scene_transform import SIDECAR_NAME, write_scene_transform_sidecar
from .vksplat_runner import validate_package


def find_gsplat_trainer(gsplat_root: Path) -> Path:
    candidates = [
        gsplat_root / "examples" / "simple_trainer.py",
        gsplat_root / "simple_trainer.py",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()
    raise FileNotFoundError(f"examples/simple_trainer.py not found under {gsplat_root}")


BASE_SCHEDULE_STEPS = 30000
RECIPE_FLAGS = ("--use_bilateral_grid", "--random_bkgd", "--steps_scaler", "--strategy.cap-max")


def probe_trainer_flags(trainer: Path, strategy: str) -> set[str]:
    try:
        completed = subprocess.run(
            [sys.executable, str(trainer), strategy, "--help"],
            capture_output=True,
            text=True,
            timeout=180,
        )
    except (OSError, subprocess.TimeoutExpired):
        return set()
    text = (completed.stdout + completed.stderr).replace("-", "_")
    return {flag for flag in RECIPE_FLAGS if flag.strip("-").replace("-", "_") in text}


def build_command(
    gsplat_root: Path,
    trainer: Path,
    package_dir: Path,
    output_root: Path,
    steps: int,
    strategy: str,
    data_factor: int,
    supported_flags: set[str] | None = None,
    use_bilateral_grid: bool = True,
    random_bkgd: bool = True,
    max_gaussians: int = 1_000_000,
) -> list[str]:
    flags = supported_flags if supported_flags is not None else set(RECIPE_FLAGS)
    # steps_scaler multiplies max/eval/save/ply steps and the refine schedule
    # inside gsplat (adjust_steps), so short rungs compress the whole schedule
    # instead of truncating a 30000-step one. Never pass both a scaled base
    # and a reduced max_steps or the run trains steps^2/30000.
    scale_schedule = "--steps_scaler" in flags and int(steps) != BASE_SCHEDULE_STEPS
    step_value = str(BASE_SCHEDULE_STEPS if scale_schedule else int(steps))
    command = [
        sys.executable,
        str(trainer),
        strategy,
        "--data_dir",
        str(package_dir),
        "--result_dir",
        str(output_root),
        "--data_factor",
        str(int(data_factor)),
        "--max_steps",
        step_value,
        "--eval_steps",
        step_value,
        "--save_steps",
        step_value,
        "--save_ply",
        "--ply_steps",
        step_value,
        "--disable_viewer",
        "--disable_video",
    ]
    if scale_schedule:
        command += ["--steps_scaler", f"{int(steps) / BASE_SCHEDULE_STEPS:.6g}"]
    if use_bilateral_grid and "--use_bilateral_grid" in flags:
        command.append("--use_bilateral_grid")
    if random_bkgd and "--random_bkgd" in flags:
        command.append("--random_bkgd")
    if strategy == "mcmc" and "--strategy.cap-max" in flags:
        command += ["--strategy.cap-max", str(int(max_gaussians))]
    return command


def find_gsplat_ply(output_root: Path, steps: int) -> Path | None:
    preferred = output_root / "ply" / f"point_cloud_{int(steps)}.ply"
    if preferred.exists():
        return preferred
    candidates = sorted((output_root / "ply").glob("point_cloud_*.ply"), key=lambda path: path.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


def run_gsplat(package_dir: Path, output_root: Path, gsplat_root: Path, steps: int = 30000, strategy: str = "mcmc", image_dir: str = "images", sparse_dir: str = "sparse/0", data_factor: int = 1, dry_run: bool = False, use_bilateral_grid: bool = True, random_bkgd: bool = True, max_gaussians: int = 1_000_000) -> dict[str, Any]:
    package_dir = package_dir.resolve()
    output_root = output_root.resolve()
    gsplat_root = gsplat_root.resolve()
    validate_package(package_dir, image_dir, sparse_dir)
    trainer = find_gsplat_trainer(gsplat_root)
    output_root.mkdir(parents=True, exist_ok=True)
    supported_flags = set(RECIPE_FLAGS) if dry_run else probe_trainer_flags(trainer, strategy)
    command = build_command(
        gsplat_root,
        trainer,
        package_dir,
        output_root,
        steps,
        strategy,
        data_factor,
        supported_flags=supported_flags,
        use_bilateral_grid=use_bilateral_grid,
        random_bkgd=random_bkgd,
        max_gaussians=max_gaussians,
    )
    summary: dict[str, Any] = {
        "schema": "capture_splat.gsplat_run_summary.v0.1",
        "package_dir": str(package_dir),
        "output_root": str(output_root),
        "gsplat_root": str(gsplat_root),
        "trainer": str(trainer),
        "steps": steps,
        "strategy": strategy,
        "data_factor": data_factor,
        "supported_recipe_flags": sorted(supported_flags),
        "unsupported_recipe_flags": sorted(set(RECIPE_FLAGS) - supported_flags),
        "command": command,
        "dry_run": dry_run,
    }
    if dry_run:
        write_json_strict(output_root / "capture_splat_gsplat_summary.json", summary)
        return summary
    completed = subprocess.run(command, cwd=str(gsplat_root), text=True)
    summary["returncode"] = completed.returncode
    splat = find_gsplat_ply(output_root, steps)
    summary["splat_ply"] = str(splat) if splat else None
    if splat is not None:
        sidecar = write_scene_transform_sidecar(splat, package_dir / sparse_dir, "gsplat", normalized=True)
        summary["scene_transform_sidecar"] = str(splat.parent / SIDECAR_NAME) if sidecar else None
    write_json_strict(output_root / "capture_splat_gsplat_summary.json", summary)
    if completed.returncode != 0:
        raise RuntimeError(f"gsplat training failed with exit code {completed.returncode}")
    if splat is None:
        raise FileNotFoundError("gsplat completed but no point_cloud_<step>.ply was found")
    return summary


def doctor(gsplat_root: Path | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema": "capture_splat.gsplat_doctor.v0.1",
        "python": sys.version.split()[0],
        "gsplat_importable": False,
        "torch_importable": False,
        "torch_cuda_available": False,
    }
    try:
        import torch  # type: ignore

        result["torch_importable"] = True
        result["torch_version"] = getattr(torch, "__version__", None)
        result["torch_cuda_available"] = bool(torch.cuda.is_available())
        if result["torch_cuda_available"]:
            result["torch_cuda_device"] = torch.cuda.get_device_name(0)
    except Exception as exc:
        result["torch_import_error"] = str(exc)
    try:
        __import__("gsplat")
        result["gsplat_importable"] = True
    except Exception as exc:
        result["gsplat_import_error"] = str(exc)
    if gsplat_root is not None:
        try:
            result["trainer"] = str(find_gsplat_trainer(gsplat_root))
        except Exception as exc:
            result["trainer_error"] = str(exc)
    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run or validate a gsplat training command for a COLMAP package.")
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--gsplat-root", type=Path, required=True)
    parser.add_argument("--steps", type=int, default=30000)
    parser.add_argument("--strategy", choices=["default", "mcmc"], default="mcmc")
    parser.add_argument("--image-dir", default="images")
    parser.add_argument("--sparse-dir", default="sparse/0")
    parser.add_argument("--data-factor", type=int, default=1)
    parser.add_argument("--no-bilateral-grid", action="store_true")
    parser.add_argument("--no-random-bkgd", action="store_true")
    parser.add_argument("--max-gaussians", type=int, default=1_000_000)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    summary = run_gsplat(args.package, args.out, args.gsplat_root, args.steps, args.strategy, args.image_dir, args.sparse_dir, args.data_factor, args.dry_run, use_bilateral_grid=not args.no_bilateral_grid, random_bkgd=not args.no_random_bkgd, max_gaussians=args.max_gaussians)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
