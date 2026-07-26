from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from .json_utils import write_json_strict

SUMMARY_SCHEMA = "capture_splat.remove_background_summary.v0.1"
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".tif", ".tiff"}


def _require_empty(path: Path) -> None:
    if path.exists() and any(path.iterdir()):
        raise FileExistsError(f"remove-background output is not empty: {path}")
    path.mkdir(parents=True, exist_ok=True)


def _source_images(images_dir: Path) -> list[Path]:
    if not images_dir.is_dir():
        raise FileNotFoundError(f"image directory not found: {images_dir}")
    images = sorted(path for path in images_dir.iterdir() if path.suffix.lower() in IMAGE_SUFFIXES)
    if not images:
        raise ValueError(f"no supported images found: {images_dir}")
    stems = [path.stem.casefold() for path in images]
    if len(stems) != len(set(stems)):
        raise ValueError("source image stems must be unique")
    return images


def _mask_path(mask_dir: Path | None, image: Path) -> Path | None:
    if mask_dir is None:
        return None
    for name in (f"{image.name}.png", f"{image.stem}.png", image.name):
        candidate = mask_dir / name
        if candidate.is_file():
            return candidate
    return None


def _prior_alpha(mask_path: Path, size: tuple[int, int], threshold: float) -> np.ndarray:
    with Image.open(mask_path) as mask_image:
        mask = np.asarray(mask_image.convert("L"), dtype=np.uint8)
    expected = (size[1], size[0])
    if mask.shape != expected:
        raise ValueError(
            f"mask dimension mismatch for {mask_path.name}: "
            f"expected {size[0]}x{size[1]}, got {mask.shape[1]}x{mask.shape[0]}"
        )
    return np.where(mask >= round(threshold * 255), 255, 0).astype(np.uint8)


def _validate_prior_masks(images: list[Path], masks: dict[Path, Path | None]) -> None:
    for image_path in images:
        mask_path = masks[image_path]
        if mask_path is None:
            raise ValueError(f"prior mask missing for {image_path.name}")
        with Image.open(image_path) as image, Image.open(mask_path) as mask:
            if image.size != mask.size:
                raise ValueError(
                    f"mask dimension mismatch for {mask_path.name}: "
                    f"expected {image.width}x{image.height}, got {mask.width}x{mask.height}"
                )


def _inspyrenet_available() -> bool:
    return importlib.util.find_spec("transparent_background") is not None


def _inspyrenet_alpha(remover: Any, image: Image.Image, threshold: float) -> np.ndarray:
    result = remover.process(image.convert("RGB"), type="rgba", threshold=threshold)
    rgba = np.asarray(result, dtype=np.uint8)
    if rgba.ndim != 3 or rgba.shape[2] != 4:
        raise RuntimeError("transparent-background returned a non-RGBA result")
    return rgba[:, :, 3]


def _write_premultiplied(image: Image.Image, alpha: np.ndarray, output: Path) -> float:
    rgb = np.asarray(image.convert("RGB"), dtype=np.uint16)
    if alpha.shape != rgb.shape[:2]:
        raise ValueError(f"alpha dimension mismatch for {output.name}")
    premultiplied = ((rgb * alpha[:, :, None].astype(np.uint16)) + 127) // 255
    rgba = np.concatenate((premultiplied.astype(np.uint8), alpha[:, :, None]), axis=2)
    output.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(rgba, mode="RGBA").save(output)
    return float(np.count_nonzero(alpha)) / float(alpha.size)


def remove_background(
    images_dir: Path,
    out_dir: Path,
    *,
    mask_dir: Path | None = None,
    mode: str = "auto",
    threshold: float = 0.5,
    model_mode: str = "fast",
    dry_run: bool = False,
) -> dict[str, Any]:
    images_dir = images_dir.resolve()
    out_dir = out_dir.resolve()
    mask_dir = mask_dir.resolve() if mask_dir is not None else None
    if mode not in {"auto", "prior", "inspyrenet"}:
        raise ValueError("mode must be auto, prior, or inspyrenet")
    if model_mode not in {"fast", "base", "base-nightly"}:
        raise ValueError("model_mode must be fast, base, or base-nightly")
    if not 0 < threshold < 1:
        raise ValueError("threshold must be between 0 and 1")

    images = _source_images(images_dir)
    masks = {image: _mask_path(mask_dir, image) for image in images}
    complete_prior = all(path is not None for path in masks.values())
    model_available = _inspyrenet_available()
    resolved_mode = mode
    if mode == "auto":
        resolved_mode = "prior" if complete_prior else "inspyrenet"
    if resolved_mode == "prior" and not complete_prior:
        missing = [image.name for image, path in masks.items() if path is None]
        raise ValueError(f"prior masks missing for {len(missing)} images: {missing[:5]}")
    if resolved_mode == "prior":
        _validate_prior_masks(images, masks)
    if resolved_mode == "inspyrenet" and not model_available and not dry_run:
        raise RuntimeError(
            "transparent_background_missing: install the optional transparent-background package"
        )

    _require_empty(out_dir)
    records: list[dict[str, Any]] = []
    remover = None
    if resolved_mode == "inspyrenet" and not dry_run:
        from transparent_background import Remover

        remover = Remover(mode=model_mode)

    for image_path in images:
        output = out_dir / "images" / f"{image_path.stem}.png"
        record: dict[str, Any] = {
            "source": image_path.name,
            "output": output.relative_to(out_dir).as_posix(),
            "mask": masks[image_path].name if masks[image_path] is not None else None,
            "method": resolved_mode,
        }
        if not dry_run:
            with Image.open(image_path) as image:
                alpha = (
                    _prior_alpha(masks[image_path], image.size, threshold)
                    if resolved_mode == "prior"
                    else _inspyrenet_alpha(remover, image, threshold)
                )
                record["foreground_fraction"] = _write_premultiplied(image, alpha, output)
        records.append(record)

    summary = {
        "schema": SUMMARY_SCHEMA,
        "source_images": str(images_dir),
        "output_dir": str(out_dir),
        "mode_requested": mode,
        "mode_resolved": resolved_mode,
        "threshold": threshold,
        "model_mode": model_mode if resolved_mode == "inspyrenet" else None,
        "model_available": model_available,
        "premultiplied_alpha": True,
        "image_count": len(images),
        "output_count": 0 if dry_run else len(records),
        "dry_run": dry_run,
        "records": records,
        "decision": "hold" if dry_run and resolved_mode == "inspyrenet" and not model_available else "ready",
        "warnings": (
            ["transparent_background_missing"]
            if dry_run and resolved_mode == "inspyrenet" and not model_available
            else []
        ),
        "authority": {
            "derived_images_are_proposals": True,
            "source_images_preserved": True,
            "quality_claim": False,
        },
    }
    write_json_strict(out_dir / "capture_splat_remove_background_summary.json", summary)
    return summary
