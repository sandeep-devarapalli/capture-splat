from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .json_utils import write_json_strict
from .render_source_qa import run_render_source_qa
from .vksplat_runner import run_vksplat


def parse_frame_list(frames: str | None) -> list[str] | None:
    if frames is None:
        return None
    parsed = [Path(item.strip()).stem for item in frames.split(",") if item.strip()]
    return parsed or None


def _relative_source(source_dir: Path, image_path: Path) -> str:
    try:
        return image_path.relative_to(source_dir).as_posix()
    except ValueError:
        return image_path.name


def _index_renders(train_json: dict[str, Any], source_dir: Path, work_dir: Path) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for split, prefix in (("train", "train"), ("val", "val")):
        for index, item in enumerate(train_json.get(f"{split}_images", [])):
            image_path = Path(item["image_path"])
            render = work_dir / f"{prefix}_{index:05d}.png"
            indexed[image_path.stem] = {
                "frame_id": image_path.stem,
                "split": split,
                "source": _relative_source(source_dir, image_path),
                "render": render.name,
                "source_path": str(image_path),
                "render_path": str(render),
                "render_exists": render.exists(),
            }
    return indexed


def run_vksplat_render_probe(
    package_dir: Path,
    output_root: Path,
    vksplat_root: Path,
    *,
    frames: str | None = None,
    steps: int = 7000,
    image_dir: str = "images",
    sparse_dir: str = "sparse/0",
    strategy: str = "mcmc",
    dry_run: bool = False,
) -> dict[str, Any]:
    package_dir = package_dir.resolve()
    output_root = output_root.resolve()
    source_dir = package_dir / image_dir
    output_root.mkdir(parents=True, exist_ok=True)
    requested_frames = parse_frame_list(frames)

    run_summary = run_vksplat(
        package_dir,
        output_root,
        vksplat_root,
        steps=steps,
        image_dir=image_dir,
        sparse_dir=sparse_dir,
        strategy=strategy,
        dry_run=dry_run,
        save_train_renders=True,
    )

    summary: dict[str, Any] = {
        "schema": "capture_splat.vksplat_render_probe.v0.1",
        "package_dir": str(package_dir),
        "output_root": str(output_root),
        "vksplat_root": str(vksplat_root.resolve()),
        "steps": steps,
        "strategy": strategy,
        "requested_frames": requested_frames,
        "run_summary": run_summary,
        "authority": {
            "source_frames": "visual_evidence",
            "renders": "model_review_proposal",
            "quality_claim": False,
        },
        "warnings": [],
    }

    if dry_run:
        summary["decision"] = "setup"
        write_json_strict(output_root / "capture_splat_vksplat_render_probe_summary.json", summary)
        return summary

    splat_ply = run_summary.get("splat_ply")
    if not splat_ply:
        raise FileNotFoundError("VkSplat probe completed without splat_ply in summary")
    work_dir = Path(str(splat_ply)).parent
    train_json_path = work_dir / "train.json"
    train_json = json.loads(train_json_path.read_text(encoding="utf-8"))
    indexed = _index_renders(train_json, source_dir, work_dir)
    frame_ids = requested_frames or sorted(indexed)

    pairs: list[dict[str, str]] = []
    coverage: list[dict[str, Any]] = []
    for frame_id in frame_ids:
        entry = indexed.get(Path(frame_id).stem)
        if entry is None:
            summary["warnings"].append(f"frame_missing:{frame_id}")
            coverage.append({"frame_id": frame_id, "available": False})
            continue
        coverage.append({key: entry[key] for key in ("frame_id", "split", "source", "render", "source_path", "render_path", "render_exists")})
        if entry["render_exists"]:
            pairs.append({"source": entry["source"], "render": entry["render"]})
        else:
            summary["warnings"].append(f"render_missing:{entry['frame_id']}")

    pairs_path = output_root / "render_source_pairs.json"
    write_json_strict(pairs_path, {"schema": "capture_splat.vksplat_render_probe_pairs.v0.1", "pairs": pairs})
    qa = run_render_source_qa(source_dir, work_dir, output_root / "render_qa", pairs_json=pairs_path)

    summary.update(
        {
            "work_dir": str(work_dir),
            "splat_ply": splat_ply,
            "render_source_pairs": str(pairs_path),
            "render_source_qa": str(output_root / "render_qa" / "capture_splat_render_source_qa_summary.json"),
            "frame_coverage": coverage,
            "qa_summary": qa,
            "decision": qa["decision"],
        }
    )
    write_json_strict(output_root / "capture_splat_vksplat_render_probe_summary.json", summary)
    return summary
