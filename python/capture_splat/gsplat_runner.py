from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from .json_utils import write_json_strict
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


def build_command(gsplat_root: Path, trainer: Path, package_dir: Path, output_root: Path, steps: int, strategy: str, data_factor: int) -> list[str]:
    return [
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
        str(int(steps)),
        "--eval_steps",
        f"[{int(steps)}]",
        "--save_steps",
        f"[{int(steps)}]",
        "--save_ply",
        "--ply_steps",
        f"[{int(steps)}]",
        "--disable_viewer",
        "--disable_video",
    ]


def find_gsplat_ply(output_root: Path, steps: int) -> Path | None:
    preferred = output_root / "ply" / f"point_cloud_{int(steps)}.ply"
    if preferred.exists():
        return preferred
    candidates = sorted((output_root / "ply").glob("point_cloud_*.ply"), key=lambda path: path.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


def run_gsplat(package_dir: Path, output_root: Path, gsplat_root: Path, steps: int = 30000, strategy: str = "mcmc", image_dir: str = "images", sparse_dir: str = "sparse/0", data_factor: int = 1, dry_run: bool = False) -> dict[str, Any]:
    package_dir = package_dir.resolve()
    output_root = output_root.resolve()
    gsplat_root = gsplat_root.resolve()
    validate_package(package_dir, image_dir, sparse_dir)
    trainer = find_gsplat_trainer(gsplat_root)
    output_root.mkdir(parents=True, exist_ok=True)
    command = build_command(gsplat_root, trainer, package_dir, output_root, steps, strategy, data_factor)
    summary: dict[str, Any] = {
        "schema": "capture_splat.gsplat_run_summary.v0.1",
        "package_dir": str(package_dir),
        "output_root": str(output_root),
        "gsplat_root": str(gsplat_root),
        "trainer": str(trainer),
        "steps": steps,
        "strategy": strategy,
        "data_factor": data_factor,
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
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    summary = run_gsplat(args.package, args.out, args.gsplat_root, args.steps, args.strategy, args.image_dir, args.sparse_dir, args.data_factor, args.dry_run)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
