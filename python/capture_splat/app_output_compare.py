from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .capture_schema import frame_selection_summary, load_capture
from .json_utils import load_json_strict, write_json_strict
from .ply_stats import inspect_ply

ARTIFACT_SUFFIXES = {".ply", ".splat", ".ksplat", ".spz", ".obj", ".glb", ".gltf", ".usdz"}
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"}
METADATA_SUFFIXES = {".json", ".csv", ".txt", ".yaml", ".yml"}


def _relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _safe_load_json(path: Path) -> dict[str, Any] | None:
    try:
        data = load_json_strict(path)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _summarize_json(path: Path, root: Path) -> dict[str, Any]:
    data = _safe_load_json(path)
    return {
        "path": _relative(path, root),
        "schema": data.get("schema") if data else None,
        "decision": data.get("decision") if data else None,
        "valid_json": data is not None,
    }


def _summarize_artifact(path: Path, root: Path) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "path": _relative(path, root),
        "suffix": path.suffix.lower(),
        "size_bytes": path.stat().st_size,
    }
    if path.suffix.lower() == ".ply":
        try:
            summary["ply_stats"] = inspect_ply(path)
            summary["finite"] = summary["ply_stats"]["finite"]
            summary["splat_count"] = summary["ply_stats"]["splat_count"]
        except Exception as exc:
            summary["ply_error"] = str(exc)
            summary["finite"] = False
    return summary


def _latest_summary(root: Path, name: str) -> dict[str, Any] | None:
    candidates = sorted(root.rglob(name), key=lambda path: path.stat().st_mtime, reverse=True)
    for candidate in candidates:
        data = _safe_load_json(candidate)
        if data is not None:
            data = dict(data)
            data["summary_path"] = _relative(candidate, root)
            return data
    return None


def summarize_app_output(label: str, path: Path) -> dict[str, Any]:
    root = path.resolve()
    if not root.exists():
        return {
            "label": label,
            "path": str(root),
            "exists": False,
            "decision": "reject",
            "warnings": ["path_missing"],
        }

    files = [item for item in root.rglob("*") if item.is_file()]
    artifacts = [_summarize_artifact(item, root) for item in files if item.suffix.lower() in ARTIFACT_SUFFIXES]
    images = [item for item in files if item.suffix.lower() in IMAGE_SUFFIXES]
    metadata = [item for item in files if item.suffix.lower() in METADATA_SUFFIXES]
    json_summaries = [_summarize_json(item, root) for item in files if item.suffix.lower() == ".json"]

    capture_manifest = root / "capture.json"
    capture_summary = None
    if capture_manifest.exists():
        try:
            capture_summary = frame_selection_summary(load_capture(root))
        except Exception as exc:
            capture_summary = {"error": str(exc)}

    render_qa = _latest_summary(root, "capture_splat_render_source_qa_summary.json")
    capture_quality = _latest_summary(root, "capture_splat_capture_quality_report.json")
    ladder = _latest_summary(root, "capture_splat_vksplat_ladder_summary.json")

    warnings = []
    if not artifacts:
        warnings.append("no_3d_output_artifact_found")
    if render_qa is None:
        warnings.append("render_source_qa_missing")
    if capture_manifest.exists() and capture_quality is None:
        warnings.append("capture_quality_report_missing")

    finite_ply_count = sum(1 for artifact in artifacts if artifact.get("suffix") == ".ply" and artifact.get("finite") is True)
    decision = "promote" if artifacts and (finite_ply_count > 0 or any(item["suffix"] != ".ply" for item in artifacts)) else "hold"
    if "no_3d_output_artifact_found" in warnings:
        decision = "reject"

    return {
        "label": label,
        "path": str(root),
        "exists": True,
        "file_count": len(files),
        "image_count": len(images),
        "metadata_file_count": len(metadata),
        "artifact_count": len(artifacts),
        "artifacts": artifacts,
        "json_summaries": json_summaries[:25],
        "capture_manifest": capture_summary,
        "capture_quality_summary": capture_quality,
        "render_source_qa_summary": render_qa,
        "vksplat_ladder_summary": ladder,
        "warnings": warnings,
        "decision": decision,
    }


def compare_app_outputs(
    out_dir: Path,
    capture_splat: Path | None = None,
    splatking: Path | None = None,
    kiri: Path | None = None,
) -> dict[str, Any]:
    inputs = {
        "capture_splat": capture_splat,
        "splatking": splatking,
        "kiri_engine": kiri,
    }
    present = {label: path for label, path in inputs.items() if path is not None}
    if len(present) < 2:
        raise ValueError("compare-app-output needs at least two app output directories")

    app_summaries = [summarize_app_output(label, path) for label, path in present.items()]
    warnings = []
    if any(app.get("render_source_qa_summary") is None for app in app_summaries):
        warnings.append("render_canvas_qa_not_available_for_all_apps")
    if any(app["decision"] == "reject" for app in app_summaries):
        warnings.append("one_or_more_app_outputs_missing_3d_artifacts")

    summary = {
        "schema": "capture_splat.app_output_comparison.v0.1",
        "apps": app_summaries,
        "comparison": {
            "app_count": len(app_summaries),
            "labels": [app["label"] for app in app_summaries],
            "artifact_counts": {app["label"]: app["artifact_count"] for app in app_summaries},
            "image_counts": {app["label"]: app["image_count"] for app in app_summaries},
            "render_qa_available": {app["label"]: app["render_source_qa_summary"] is not None for app in app_summaries},
        },
        "warnings": warnings,
        "decision": "hold" if warnings else "promote",
        "authority": {
            "observable_artifacts_only": True,
            "reverse_engineers_proprietary_internals": False,
            "quality_proxy": True,
            "metric_authority": False,
            "training_result": False,
        },
    }
    write_json_strict(out_dir / "capture_splat_app_output_comparison.json", summary)
    return summary


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare observable outputs from iPhone 3DGS apps.")
    parser.add_argument("--capture-splat", type=Path)
    parser.add_argument("--splatking", type=Path)
    parser.add_argument("--kiri", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    summary = compare_app_outputs(
        args.out,
        capture_splat=args.capture_splat,
        splatking=args.splatking,
        kiri=args.kiri,
    )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
