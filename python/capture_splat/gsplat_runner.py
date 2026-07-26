from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from .json_utils import load_json_strict, write_json_strict
from .scene_transform import SIDECAR_NAME, resolve_normalization_policy, write_scene_transform_sidecar
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
RECIPE_FLAGS = (
    "--random_bkgd",
    "--steps_scaler",
    "--strategy.cap-max",
    "--strategy.refine-every",
)
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".tif", ".tiff"}


def _probe_import(trainer: Path, statement: str) -> dict[str, Any]:
    command = [sys.executable, "-c", statement]
    try:
        completed = subprocess.run(command, cwd=str(trainer.parent), capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"ready": False, "command": command, "error": str(error)}
    return {
        "ready": completed.returncode == 0,
        "command": command,
        "returncode": completed.returncode,
        "error": ((completed.stderr or "") + (completed.stdout or ""))[-1000:] if completed.returncode else None,
    }


def probe_trainer_capabilities(trainer: Path, strategy: str) -> dict[str, Any]:
    command = [sys.executable, str(trainer), strategy, "--help"]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=180)
        help_text = (completed.stdout or "") + (completed.stderr or "")
        returncode = completed.returncode
    except (OSError, subprocess.TimeoutExpired) as error:
        help_text = ""
        returncode = None
        help_error = str(error)
    source = trainer.read_text(encoding="utf-8", errors="ignore")
    normalized = help_text.replace("-", "_")
    flags = {
        flag for flag in RECIPE_FLAGS
        if flag.strip("-").replace("-", "_") in normalized
        or flag.strip("-").replace("-", "_") in source
    }
    modern_post = "--post-processing" in help_text or "post_processing:" in source
    legacy_bilateral = "--use-bilateral-grid" in help_text or "--use_bilateral_grid" in help_text
    normalization_disable = (
        "--no-normalize-world-space"
        if "--no-normalize-world-space" in help_text or "normalize_world_space: bool = True" in source
        else None
    )
    choices = []
    if modern_post:
        if "bilateral_grid" in help_text or "bilateral_grid" in source:
            choices.append("bilateral_grid")
        if "ppisp" in help_text or '"ppisp"' in source:
            choices.append("ppisp")
    result: dict[str, Any] = {
        "help_command": command,
        "help_returncode": returncode,
        "help_error": locals().get("help_error"),
        "supported_recipe_flags": sorted(flags),
        "post_processing_option": "--post-processing" if modern_post else None,
        "post_processing_choices": choices,
        "legacy_bilateral_grid": legacy_bilateral,
        "mask_dir_option": "--mask-dir" if "--mask-dir" in help_text else None,
        "normalization_disable_option": normalization_disable,
        "source_probe_used": returncode != 0,
        "dependencies": {
            "bilateral_grid": _probe_import(trainer, "import lib_bilagrid"),
            "ppisp": _probe_import(trainer, "import ppisp, ppisp.report"),
        },
    }
    return result


def probe_trainer_flags(trainer: Path, strategy: str) -> set[str]:
    return set(probe_trainer_capabilities(trainer, strategy)["supported_recipe_flags"])


def default_photometric_mode(package_dir: Path) -> str:
    manifest = package_dir / "capture.json"
    if not manifest.exists():
        return "none"
    try:
        capture = load_json_strict(manifest)
    except (OSError, ValueError):
        return "none"
    return "bilateral-grid" if capture.get("source") == "capture_splat.prepare_capture" else "none"


def resolve_mcmc_refine_every(
    package_dir: Path,
    image_dir: str,
    steps: int,
    strategy: str,
    requested: str | int,
    supported_flags: set[str],
) -> dict[str, Any]:
    requested_text = str(requested).strip().lower()
    if strategy != "mcmc":
        if requested_text != "auto":
            raise ValueError("--mcmc-refine-every only applies to the mcmc strategy")
        return {"requested": "auto", "applied": False, "reason": "strategy_not_mcmc"}
    if "--strategy.refine-every" not in supported_flags:
        raise RuntimeError("gsplat trainer does not expose --strategy.refine-every")

    frame_count = sum(
        1
        for path in (package_dir / image_dir).iterdir()
        if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES
    )
    if requested_text == "auto":
        target = max(200, round(frame_count / 100) * 100)
        source = "auto_frame_count"
    else:
        try:
            target = int(requested_text)
        except ValueError as error:
            raise ValueError("--mcmc-refine-every must be auto or a positive integer") from error
        if target <= 0:
            raise ValueError("--mcmc-refine-every must be auto or a positive integer")
        source = "explicit"

    scale = float(f"{int(steps) / BASE_SCHEDULE_STEPS:.6g}") if int(steps) != BASE_SCHEDULE_STEPS else 1.0
    command_value = max(1, math.ceil(target / scale))
    while int(command_value * scale) < target:
        command_value += 1
    return {
        "requested": requested_text,
        "applied": True,
        "source": source,
        "frame_count": frame_count,
        "target_effective_steps": target,
        "trainer_command_value": command_value,
        "schedule_scale": scale,
        "expected_effective_steps": int(command_value * scale),
    }


def build_command(
    gsplat_root: Path,
    trainer: Path,
    package_dir: Path,
    output_root: Path,
    steps: int,
    strategy: str,
    data_factor: int,
    supported_flags: set[str] | None = None,
    photometric: str = "bilateral-grid",
    capabilities: dict[str, Any] | None = None,
    random_bkgd: bool = True,
    max_gaussians: int = 1_000_000,
    mask_dir: Path | None = None,
    normalize_world_space: bool = True,
    mcmc_refine_every_command: int | None = None,
) -> list[str]:
    flags = supported_flags if supported_flags is not None else set(RECIPE_FLAGS)
    capabilities = capabilities or {
        "post_processing_option": "--post-processing",
        "post_processing_choices": ["bilateral_grid", "ppisp"],
        "legacy_bilateral_grid": False,
    }
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
    if photometric != "none":
        value = photometric.replace("-", "_")
        if value in capabilities.get("post_processing_choices", []):
            command += [str(capabilities["post_processing_option"]), value]
        elif value == "bilateral_grid" and capabilities.get("legacy_bilateral_grid"):
            command.append("--use-bilateral-grid")
        else:
            raise RuntimeError(f"gsplat trainer does not support photometric mode: {photometric}")
    if random_bkgd and "--random_bkgd" in flags:
        command.append("--random_bkgd")
    if strategy == "mcmc" and "--strategy.cap-max" in flags:
        command += ["--strategy.cap-max", str(int(max_gaussians))]
    if strategy == "mcmc" and mcmc_refine_every_command is not None:
        command += ["--strategy.refine-every", str(int(mcmc_refine_every_command))]
    if mask_dir is not None:
        option = capabilities.get("mask_dir_option")
        if not option:
            raise RuntimeError("gsplat trainer does not expose a mask directory option")
        command += [str(option), str(mask_dir)]
    if not normalize_world_space:
        option = capabilities.get("normalization_disable_option")
        if not option:
            raise RuntimeError("gsplat trainer does not expose a normalization-disable option")
        command.append(str(option))
    return command


def find_gsplat_ply(output_root: Path, steps: int) -> Path | None:
    preferred = output_root / "ply" / f"point_cloud_{int(steps)}.ply"
    if preferred.exists():
        return preferred
    candidates = sorted((output_root / "ply").glob("point_cloud_*.ply"), key=lambda path: path.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


def run_gsplat(package_dir: Path, output_root: Path, gsplat_root: Path, steps: int = 30000, strategy: str = "mcmc", image_dir: str = "images", sparse_dir: str = "sparse/0", data_factor: int = 1, dry_run: bool = False, use_bilateral_grid: bool | None = None, random_bkgd: bool = True, max_gaussians: int = 1_000_000, photometric: str | None = None, masks: str = "auto", normalization: str = "auto", mcmc_refine_every: str | int = "auto") -> dict[str, Any]:
    package_dir = package_dir.resolve()
    output_root = output_root.resolve()
    gsplat_root = gsplat_root.resolve()
    validate_package(package_dir, image_dir, sparse_dir)
    trainer = find_gsplat_trainer(gsplat_root)
    output_root.mkdir(parents=True, exist_ok=True)
    if photometric is None:
        if use_bilateral_grid is not None:
            photometric = "bilateral-grid" if use_bilateral_grid else "none"
        else:
            photometric = default_photometric_mode(package_dir)
    if photometric not in {"none", "bilateral-grid", "ppisp"}:
        raise ValueError(f"unsupported photometric mode: {photometric}")
    if masks not in {"auto", "off", "required"}:
        raise ValueError(f"unsupported mask policy: {masks}")
    if photometric == "ppisp" and strategy != "mcmc":
        raise RuntimeError("gsplat PPISP requires the mcmc strategy")
    capabilities = probe_trainer_capabilities(trainer, strategy)
    supported_flags = set(capabilities["supported_recipe_flags"])
    refine_every_state = resolve_mcmc_refine_every(
        package_dir,
        image_dir,
        steps,
        strategy,
        mcmc_refine_every,
        supported_flags,
    )
    normalization_state = resolve_normalization_policy(
        package_dir,
        sparse_dir,
        normalization,
        capabilities.get("normalization_disable_option") is not None,
    )
    mask_dir = package_dir / "masks" / "valid"
    mask_files = sorted(mask_dir.glob("*.png")) if mask_dir.is_dir() else []
    image_names = sorted(path.name for path in (package_dir / image_dir).iterdir() if path.suffix.lower() in {".jpg", ".jpeg", ".png", ".tif", ".tiff"})
    missing_masks = [name for name in image_names if not (mask_dir / f"{name}.png").exists()]
    mask_supported = capabilities.get("mask_dir_option") is not None
    mask_complete = bool(mask_files) and not missing_masks
    if masks == "required" and (not mask_complete or not mask_supported):
        raise RuntimeError("required masks are unavailable or unsupported by this gsplat trainer")
    resolved_mask_dir = mask_dir if masks != "off" and mask_supported and mask_complete else None
    dependency_name = photometric.replace("-", "_")
    dependency = capabilities.get("dependencies", {}).get(dependency_name)
    if not dry_run and photometric != "none" and isinstance(dependency, dict) and not dependency.get("ready"):
        raise RuntimeError(f"gsplat {photometric} dependency is not importable")
    command = build_command(
        gsplat_root,
        trainer,
        package_dir,
        output_root,
        steps,
        strategy,
        data_factor,
        supported_flags=supported_flags,
        photometric=photometric,
        capabilities=capabilities,
        random_bkgd=random_bkgd,
        max_gaussians=max_gaussians,
        mask_dir=resolved_mask_dir,
        normalize_world_space=normalization_state["enabled"],
        mcmc_refine_every_command=refine_every_state.get("trainer_command_value"),
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
        "photometric": photometric,
        "trainer_capabilities": capabilities,
        "normalization": normalization_state,
        "mcmc_refine_every": refine_every_state,
        "masks": {
            "requested": masks,
            "available": len(mask_files),
            "missing": missing_masks,
            "complete": mask_complete,
            "supported": mask_supported,
            "applied": resolved_mask_dir is not None,
            "warning": (
                "gsplat_colmap_mask_input_unsupported" if mask_files and not mask_supported and masks == "auto"
                else "incomplete_valid_masks_disabled" if mask_files and missing_masks and masks == "auto"
                else None
            ),
        },
        "supported_recipe_flags": sorted(supported_flags),
        "unsupported_recipe_flags": sorted(set(RECIPE_FLAGS) - supported_flags),
        "command": command,
        "dry_run": dry_run,
        "fixed_camera_evaluation_set": str(package_dir / "metadata" / "fixed_camera_evaluation_set.json") if (package_dir / "metadata" / "fixed_camera_evaluation_set.json").exists() else None,
    }
    if dry_run:
        write_json_strict(output_root / "capture_splat_gsplat_summary.json", summary)
        return summary
    completed = subprocess.run(command, cwd=str(gsplat_root), text=True)
    summary["returncode"] = completed.returncode
    splat = find_gsplat_ply(output_root, steps)
    summary["splat_ply"] = str(splat) if splat else None
    if splat is not None:
        sidecar = write_scene_transform_sidecar(
            splat,
            package_dir / sparse_dir,
            "gsplat",
            normalized=normalization_state["enabled"],
        )
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
            trainer = find_gsplat_trainer(gsplat_root)
            result["trainer"] = str(trainer)
            result["trainer_capabilities"] = probe_trainer_capabilities(trainer, "mcmc")
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
    parser.add_argument("--photometric", choices=["none", "bilateral-grid", "ppisp"])
    parser.add_argument("--masks", choices=["auto", "off", "required"], default="auto")
    parser.add_argument("--normalization", choices=["auto", "on", "off"], default="auto")
    parser.add_argument("--no-bilateral-grid", action="store_true")
    parser.add_argument("--no-random-bkgd", action="store_true")
    parser.add_argument("--max-gaussians", type=int, default=1_000_000)
    parser.add_argument("--mcmc-refine-every", default="auto", metavar="auto|N")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    photometric = "none" if args.no_bilateral_grid else args.photometric
    summary = run_gsplat(args.package, args.out, args.gsplat_root, args.steps, args.strategy, args.image_dir, args.sparse_dir, args.data_factor, args.dry_run, random_bkgd=not args.no_random_bkgd, max_gaussians=args.max_gaussians, photometric=photometric, masks=args.masks, normalization=args.normalization, mcmc_refine_every=args.mcmc_refine_every)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
