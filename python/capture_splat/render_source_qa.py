from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from .json_utils import load_json_strict, write_json_strict

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp"}


def image_paths(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*") if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES)


def load_pairs(source_dir: Path, render_dir: Path, pairs_json: Path | None = None) -> list[tuple[Path, Path, str]]:
    source_dir = source_dir.resolve()
    render_dir = render_dir.resolve()
    if pairs_json is not None:
        data = load_json_strict(pairs_json)
        pairs = data.get("pairs") if isinstance(data, dict) else data
        if not isinstance(pairs, list):
            raise ValueError("pairs JSON must be a list or contain a pairs list")
        result: list[tuple[Path, Path, str]] = []
        for index, pair in enumerate(pairs, start=1):
            if not isinstance(pair, dict):
                raise ValueError(f"pair {index} must be an object")
            source_name = pair.get("source")
            render_name = pair.get("render")
            if not isinstance(source_name, str) or not isinstance(render_name, str):
                raise ValueError(f"pair {index} must contain source and render strings")
            frame_id = str(pair.get("frame_id") or Path(source_name).stem)
            result.append((source_dir / source_name, render_dir / render_name, frame_id))
        return result

    renders_by_relative = {path.relative_to(render_dir).as_posix(): path for path in image_paths(render_dir)}
    renders_by_stem: dict[str, list[Path]] = {}
    for path in renders_by_relative.values():
        renders_by_stem.setdefault(path.stem, []).append(path)

    result = []
    for source in image_paths(source_dir):
        relative = source.relative_to(source_dir).as_posix()
        render = renders_by_relative.get(relative)
        if render is None:
            candidates = renders_by_stem.get(source.stem, [])
            if len(candidates) == 1:
                render = candidates[0]
        if render is not None:
            result.append((source, render, Path(relative).stem))
    return result


def _load_rgb(path: Path) -> np.ndarray:
    with Image.open(path) as image:
        return np.asarray(image.convert("RGB"), dtype=np.float64) / 255.0


def _grayscale(image: np.ndarray) -> np.ndarray:
    return image[..., 0] * 0.299 + image[..., 1] * 0.587 + image[..., 2] * 0.114


def _edge_density(gray: np.ndarray) -> float:
    if gray.shape[0] < 2 or gray.shape[1] < 2:
        return 0.0
    gx = np.abs(np.diff(gray, axis=1))
    gy = np.abs(np.diff(gray, axis=0))
    return float((gx.mean() + gy.mean()) * 0.5)


def _laplacian_variance(gray: np.ndarray) -> float:
    if gray.shape[0] < 3 or gray.shape[1] < 3:
        return 0.0
    center = gray[1:-1, 1:-1] * 4.0
    lap = center - gray[:-2, 1:-1] - gray[2:, 1:-1] - gray[1:-1, :-2] - gray[1:-1, 2:]
    return float(np.var(lap))


def _ssim_global(a: np.ndarray, b: np.ndarray) -> float:
    c1 = 0.01**2
    c2 = 0.03**2
    mu_a = float(a.mean())
    mu_b = float(b.mean())
    var_a = float(a.var())
    var_b = float(b.var())
    cov = float(((a - mu_a) * (b - mu_b)).mean())
    denom = (mu_a * mu_a + mu_b * mu_b + c1) * (var_a + var_b + c2)
    if denom == 0.0:
        return 1.0 if np.array_equal(a, b) else 0.0
    return float(((2.0 * mu_a * mu_b + c1) * (2.0 * cov + c2)) / denom)


def _normalized_correlation(a: np.ndarray, b: np.ndarray) -> float:
    va = a - a.mean()
    vb = b - b.mean()
    denom = float(np.sqrt(np.sum(va * va) * np.sum(vb * vb)))
    if denom == 0.0:
        return 1.0 if np.array_equal(a, b) else 0.0
    return float(np.sum(va * vb) / denom)


def compare_pair(source_path: Path, render_path: Path) -> dict[str, Any]:
    if not source_path.exists():
        raise FileNotFoundError(f"source image missing: {source_path}")
    if not render_path.exists():
        raise FileNotFoundError(f"render image missing: {render_path}")
    source = _load_rgb(source_path)
    render = _load_rgb(render_path)
    if source.shape != render.shape:
        raise ValueError(f"dimension mismatch: {source_path} {source.shape[1]}x{source.shape[0]} vs {render_path} {render.shape[1]}x{render.shape[0]}")

    diff = render - source
    mae = float(np.mean(np.abs(diff)))
    mse = float(np.mean(diff * diff))
    rmse = float(math.sqrt(mse))
    psnr = 99.0 if mse == 0.0 else min(99.0, float(20.0 * math.log10(1.0 / math.sqrt(mse))))
    source_gray = _grayscale(source)
    render_gray = _grayscale(render)
    metrics = {
        "mae": mae,
        "rmse": rmse,
        "psnr": psnr,
        "ssim": _ssim_global(source_gray, render_gray),
        "normalized_correlation": _normalized_correlation(source_gray, render_gray),
        "source_edge_density": _edge_density(source_gray),
        "render_edge_density": _edge_density(render_gray),
        "source_laplacian_variance": _laplacian_variance(source_gray),
        "render_laplacian_variance": _laplacian_variance(render_gray),
    }
    for key, value in metrics.items():
        if not math.isfinite(value):
            raise ValueError(f"non-finite metric {key} for {source_path} vs {render_path}: {value}")
    return metrics


def _stats(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"mean": None, "min": None, "max": None}
    return {
        "mean": float(np.mean(values)),
        "min": float(np.min(values)),
        "max": float(np.max(values)),
    }


def run_render_source_qa(
    source_dir: Path,
    render_dir: Path,
    out_dir: Path,
    pairs_json: Path | None = None,
    min_psnr: float = 20.0,
    min_ssim: float = 0.85,
    max_mae: float = 0.08,
    min_correlation: float = 0.75,
    tail_fraction: float = 0.25,
) -> dict[str, Any]:
    source_dir = source_dir.resolve()
    render_dir = render_dir.resolve()
    out_dir = out_dir.resolve()
    pairs = load_pairs(source_dir, render_dir, pairs_json)
    warnings: list[str] = []
    errors: list[str] = []
    frames: list[dict[str, Any]] = []
    if not pairs:
        errors.append("no source/render pairs found")

    source_count = len(image_paths(source_dir)) if source_dir.exists() else 0
    render_count = len(image_paths(render_dir)) if render_dir.exists() else 0
    if source_count != render_count and pairs_json is None:
        warnings.append(f"image count differs: source={source_count} render={render_count}")

    for source_path, render_path, frame_id in pairs:
        frame: dict[str, Any] = {
            "frame_id": frame_id,
            "source": str(source_path),
            "render": str(render_path),
        }
        try:
            metrics = compare_pair(source_path, render_path)
            weak_reasons = []
            if metrics["psnr"] < min_psnr:
                weak_reasons.append("psnr_below_threshold")
            if metrics["ssim"] < min_ssim:
                weak_reasons.append("ssim_below_threshold")
            if metrics["mae"] > max_mae:
                weak_reasons.append("mae_above_threshold")
            if metrics["normalized_correlation"] < min_correlation:
                weak_reasons.append("correlation_below_threshold")
            frame.update(metrics)
            frame["decision"] = "hold" if weak_reasons else "promote"
            frame["weak_reasons"] = weak_reasons
        except Exception as exc:
            frame["decision"] = "reject"
            frame["error"] = str(exc)
            errors.append(str(exc))
        frames.append(frame)

    valid_frames = [frame for frame in frames if frame.get("decision") != "reject"]
    weak_frames = [frame["frame_id"] for frame in valid_frames if frame.get("weak_reasons")]
    metric_names = ("mae", "rmse", "psnr", "ssim", "normalized_correlation", "source_edge_density", "render_edge_density", "source_laplacian_variance", "render_laplacian_variance")
    aggregates = {
        name: _stats([float(frame[name]) for frame in valid_frames if isinstance(frame.get(name), (float, int))])
        for name in metric_names
    }

    tail_count = max(1, math.ceil(len(valid_frames) * tail_fraction)) if valid_frames else 0
    tail_frames = sorted(valid_frames, key=lambda frame: (float(frame.get("psnr", -1.0)), float(frame.get("ssim", -1.0))))[:tail_count]
    if errors:
        decision = "reject"
    elif weak_frames:
        decision = "hold"
    else:
        decision = "promote"

    summary = {
        "schema": "capture_splat.render_source_qa.v0.1",
        "source_dir": str(source_dir),
        "render_dir": str(render_dir),
        "pairs_json": str(pairs_json.resolve()) if pairs_json else None,
        "frame_count": len(frames),
        "valid_frame_count": len(valid_frames),
        "thresholds": {
            "min_psnr": min_psnr,
            "min_ssim": min_ssim,
            "max_mae": max_mae,
            "min_correlation": min_correlation,
            "tail_fraction": tail_fraction,
        },
        "aggregates": aggregates,
        "weak_frames": weak_frames,
        "tail_frames": [frame["frame_id"] for frame in tail_frames],
        "warnings": warnings,
        "errors": errors,
        "decision": decision,
        "frames": frames,
    }
    write_json_strict(out_dir / "capture_splat_render_source_qa_summary.json", summary)
    return summary


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare raw render canvases against source images.")
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--render-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--pairs-json", type=Path)
    parser.add_argument("--min-psnr", type=float, default=20.0)
    parser.add_argument("--min-ssim", type=float, default=0.85)
    parser.add_argument("--max-mae", type=float, default=0.08)
    parser.add_argument("--min-correlation", type=float, default=0.75)
    parser.add_argument("--tail-fraction", type=float, default=0.25)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    summary = run_render_source_qa(
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
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
