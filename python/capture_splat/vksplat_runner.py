from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from .json_utils import write_json_strict
from .scene_transform import SIDECAR_NAME, resolve_normalization_policy, write_scene_transform_sidecar
from .training_supervision import resolve_supervision_policy


def find_simple_trainer(vksplat_root: Path) -> Path:
    candidates = [
        vksplat_root / "simple_trainer.py",
        vksplat_root / "vksplat" / "simple_trainer.py",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()
    raise FileNotFoundError(f"simple_trainer.py not found under {vksplat_root}")


def validate_package(package_dir: Path, image_dir: str, sparse_dir: str) -> None:
    if not (package_dir / image_dir).exists():
        raise FileNotFoundError(f"image directory missing: {package_dir / image_dir}")
    sparse = package_dir / sparse_dir
    for name in ("cameras.txt", "images.txt"):
        if not (sparse / name).exists():
            raise FileNotFoundError(f"COLMAP text file missing: {sparse / name}")


def build_runner_script(path: Path, simple_trainer: Path, package_dir: Path, output_root: Path, image_dir: str, sparse_dir: str, steps: int, strategy: str, save_train_renders: bool = False, stop_reset_at: int | None = None, mask_dir: str | None = None, sensor_depth_manifest: str | None = None, sensor_normal_manifest: str | None = None) -> None:
    trainer_dir = simple_trainer.parent
    config_class = "MCMCTrainerConfig" if strategy == "mcmc" else "TrainerConfig"
    lines = [
        "import sys",
        "sys.path.insert(0, %r)" % str(trainer_dir),
        "from simple_trainer import %s, train_main" % config_class,
        "config = %s()" % config_class,
        "config.dataset_dir = %r" % str(package_dir),
        "config.image_dir = %r" % image_dir,
        "config.sparse_dir = %r" % sparse_dir,
        "config.output_dir = %r" % str(output_root),
        "config.train_steps = %d" % int(steps),
        "config.max_steps = %d" % int(steps),
        "config.enable_viewer = False",
        "config.save_train_renders = %r" % save_train_renders,
    ]
    if mask_dir is not None:
        lines.append("config.mask_dir = %r" % mask_dir)
    if sensor_depth_manifest is not None:
        lines.append("config.sensor_depth_manifest = %r" % sensor_depth_manifest)
    if sensor_normal_manifest is not None:
        lines.append("config.sensor_normal_manifest = %r" % sensor_normal_manifest)
    if stop_reset_at is not None:
        lines.append("config.stop_reset_at = %d" % int(stop_reset_at))
    lines.extend([
        "train_main(config)",
    ])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def find_latest_splat(output_root: Path) -> Path | None:
    candidates = sorted(output_root.glob("*/splat.ply"), key=lambda path: path.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


def run_vksplat(package_dir: Path, output_root: Path, vksplat_root: Path, steps: int = 30000, image_dir: str = "images", sparse_dir: str = "sparse/0", strategy: str = "mcmc", dry_run: bool = False, save_train_renders: bool = False, stop_reset_at: int | None = None, masks: str = "auto", normalization: str = "auto", depth_supervision: str = "auto", normal_supervision: str = "auto") -> dict[str, Any]:
    package_dir = package_dir.resolve()
    output_root = output_root.resolve()
    vksplat_root = vksplat_root.resolve()
    validate_package(package_dir, image_dir, sparse_dir)
    simple_trainer = find_simple_trainer(vksplat_root)
    normalization_state = resolve_normalization_policy(
        package_dir,
        sparse_dir,
        normalization,
        backend_supports_disable=False,
    )
    if masks not in {"auto", "off", "required"}:
        raise ValueError(f"unsupported mask policy: {masks}")
    trainer_source = simple_trainer.read_text(encoding="utf-8", errors="ignore")
    mask_supported = "mask_dir" in trainer_source
    depth_option = "sensor_depth_manifest" if "sensor_depth_manifest" in trainer_source else None
    normal_option = "sensor_normal_manifest" if "sensor_normal_manifest" in trainer_source else None
    depth_state = resolve_supervision_policy(package_dir, depth_supervision, "depth", depth_option)
    normal_state = resolve_supervision_policy(package_dir, normal_supervision, "normal", normal_option)
    mask_path = package_dir / "masks" / "valid"
    mask_files = sorted(mask_path.glob("*.png")) if mask_path.is_dir() else []
    image_names = sorted(path.name for path in (package_dir / image_dir).iterdir() if path.suffix.lower() in {".jpg", ".jpeg", ".png", ".tif", ".tiff"})
    missing_masks = [name for name in image_names if not (mask_path / f"{name}.png").exists()]
    mask_complete = bool(mask_files) and not missing_masks
    if masks == "required" and (not mask_supported or not mask_complete):
        raise RuntimeError("required masks are unavailable or unsupported by this VkSplat trainer")
    resolved_mask_dir = "masks/valid" if masks != "off" and mask_supported and mask_complete else None
    output_root.mkdir(parents=True, exist_ok=True)
    runner = output_root / "capture_splat_vksplat_runner.py"
    build_runner_script(
        runner,
        simple_trainer,
        package_dir,
        output_root,
        image_dir,
        sparse_dir,
        steps,
        strategy,
        save_train_renders=save_train_renders,
        stop_reset_at=stop_reset_at,
        mask_dir=resolved_mask_dir,
        sensor_depth_manifest=depth_state["manifest"] if depth_state["applied"] else None,
        sensor_normal_manifest=normal_state["manifest"] if normal_state["applied"] else None,
    )
    command = [sys.executable, str(runner)]
    summary: dict[str, Any] = {
        "schema": "capture_splat.vksplat_run_summary.v0.1",
        "package_dir": str(package_dir),
        "output_root": str(output_root),
        "vksplat_root": str(vksplat_root),
        "steps": steps,
        "strategy": strategy,
        "save_train_renders": save_train_renders,
        "stop_reset_at": stop_reset_at,
        "normalization": normalization_state,
        "masks": {
            "requested": masks,
            "available": len(mask_files),
            "missing": missing_masks,
            "complete": mask_complete,
            "supported": mask_supported,
            "applied": resolved_mask_dir is not None,
            "mask_dir": resolved_mask_dir,
            "semantics": "white_valid_for_training",
            "warning": "incomplete_valid_masks_disabled" if mask_files and missing_masks and masks == "auto" else None,
        },
        "sensor_supervision": {
            "depth": depth_state,
            "normal": normal_state,
        },
        "command": command,
        "dry_run": dry_run,
        "fixed_camera_evaluation_set": str(package_dir / "metadata" / "fixed_camera_evaluation_set.json") if (package_dir / "metadata" / "fixed_camera_evaluation_set.json").exists() else None,
    }
    if dry_run:
        write_json_strict(output_root / "capture_splat_vksplat_summary.json", summary)
        return summary
    completed = subprocess.run(command, cwd=str(simple_trainer.parent), text=True)
    summary["returncode"] = completed.returncode
    splat = find_latest_splat(output_root)
    summary["splat_ply"] = str(splat) if splat else None
    if splat is not None:
        sidecar = write_scene_transform_sidecar(
            splat,
            package_dir / sparse_dir,
            "vksplat",
            normalized=normalization_state["enabled"],
        )
        summary["scene_transform_sidecar"] = str(splat.parent / SIDECAR_NAME) if sidecar else None
    write_json_strict(output_root / "capture_splat_vksplat_summary.json", summary)
    if completed.returncode != 0:
        raise RuntimeError(f"VkSplat training failed with exit code {completed.returncode}")
    if splat is None:
        raise FileNotFoundError("VkSplat completed but no splat.ply was found")
    return summary


def doctor(vksplat_root: Path | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema": "capture_splat.doctor.v0.1",
        "python": sys.version.split()[0],
        "colmap": shutil.which("colmap"),
        "vulkaninfo": shutil.which("vulkaninfo"),
        "vksplat_importable": False,
        "normalization_disable_supported": False,
    }
    try:
        __import__("vksplat")
        result["vksplat_importable"] = True
    except Exception as exc:
        result["vksplat_import_error"] = str(exc)
    if vksplat_root is not None:
        try:
            trainer = find_simple_trainer(vksplat_root)
            result["simple_trainer"] = str(trainer)
            source = trainer.read_text(encoding="utf-8", errors="ignore")
            result["mask_dir_supported"] = "mask_dir" in source
            result["sensor_depth_manifest_supported"] = "sensor_depth_manifest" in source
            result["sensor_normal_manifest_supported"] = "sensor_normal_manifest" in source
        except Exception as exc:
            result["simple_trainer_error"] = str(exc)
    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run or validate a VkSplat training command for a COLMAP package.")
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--vksplat-root", type=Path, required=True)
    parser.add_argument("--steps", type=int, default=30000)
    parser.add_argument("--image-dir", default="images")
    parser.add_argument("--sparse-dir", default="sparse/0")
    parser.add_argument("--strategy", choices=["default", "mcmc"], default="mcmc")
    parser.add_argument("--save-train-renders", action="store_true")
    parser.add_argument("--stop-reset-at", type=int, help="Stop VkSplat opacity resets after this step; useful for longer quality rungs that otherwise destabilize.")
    parser.add_argument("--masks", choices=["auto", "off", "required"], default="auto")
    parser.add_argument("--normalization", choices=["auto", "on", "off"], default="auto")
    parser.add_argument("--depth-supervision", choices=["auto", "off", "required"], default="auto")
    parser.add_argument("--normal-supervision", choices=["auto", "off", "required"], default="auto")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    summary = run_vksplat(args.package, args.out, args.vksplat_root, args.steps, args.image_dir, args.sparse_dir, args.strategy, args.dry_run, save_train_renders=args.save_train_renders, stop_reset_at=args.stop_reset_at, masks=args.masks, normalization=args.normalization, depth_supervision=args.depth_supervision, normal_supervision=args.normal_supervision)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
