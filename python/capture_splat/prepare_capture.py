from __future__ import annotations

import json
import shutil
import tempfile
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from .capture_quality_report import run_capture_quality_report
from .capture_schema import iter_frames, load_capture
from .frames_extract import photometric_from_entry, run_extract_frames
from .json_utils import load_json_strict, write_json_strict
from .reconstruction_recipe import RECIPES, plan_reconstruction, resolve_recipe
from .sfm_evidence import camera_evidence_report, load_frame_evidence, photometric_evidence_report

SUMMARY_SCHEMA = "capture_splat.prepare_capture_summary.v0.1"
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".tif", ".tiff"}


@dataclass(frozen=True)
class Candidate:
    root: Path
    frame: dict[str, Any]
    source_kind: str
    source_index: int
    timestamp: float | None
    timestamp_domain: str


def _require_empty_output(path: Path) -> None:
    if path.exists() and any(path.iterdir()):
        raise FileExistsError(f"prepare-capture output is not empty: {path}")
    path.mkdir(parents=True, exist_ok=True)


def _accepted(frame: dict[str, Any]) -> bool:
    if frame.get("accepted") is False:
        return False
    quality = frame.get("capture_quality") or frame.get("quality")
    return not isinstance(quality, dict) or quality.get("accepted") is not False


def _candidates(root: Path, capture: dict[str, Any], source_kind: str) -> list[Candidate]:
    parsed = {item.source_index for item in iter_frames(capture, accepted_only=True)}
    result: list[Candidate] = []
    for source_index, frame in enumerate(capture["frames"], start=1):
        if source_index not in parsed or not isinstance(frame, dict) or not _accepted(frame):
            continue
        timestamp = float(frame["timestamp"]) if frame.get("timestamp") is not None else None
        default_domain = "ar_session" if source_kind == "accepted_rgbd" else "video_relative"
        result.append(Candidate(
            root=root,
            frame=frame,
            source_kind=source_kind,
            source_index=source_index,
            timestamp=timestamp,
            timestamp_domain=str(frame.get("timestamp_domain", default_domain)),
        ))
    return result


def _load_json_lines(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        value = json.loads(line)
        if isinstance(value, dict):
            records.append(value)
    return records


def _nearest(records: list[dict[str, Any]], timestamp: float | None, tolerance: float) -> dict[str, Any] | None:
    if timestamp is None:
        return None
    matches: list[tuple[float, dict[str, Any]]] = []
    for record in records:
        value = record.get("ar_timestamp", record.get("timestamp"))
        if value is None:
            continue
        distance = abs(float(value) - timestamp)
        if distance <= tolerance:
            matches.append((distance, record))
    return min(matches, key=lambda item: item[0])[1] if matches else None


def _object_records(capture_dir: Path, capture: dict[str, Any]) -> list[dict[str, Any]]:
    relative = str(capture.get("object_matte_file", "metadata/object_matte_report.json"))
    path = capture_dir / relative
    if not path.exists():
        return []
    records = load_json_strict(path).get("frame_records", [])
    return [record for record in records if isinstance(record, dict)] if isinstance(records, list) else []


def _object_record(
    records: list[dict[str, Any]], frame: dict[str, Any], timestamp: float | None
) -> dict[str, Any] | None:
    rgb = frame.get("rgb")
    if isinstance(rgb, str):
        exact = next((record for record in records if record.get("rgb") == rgb), None)
        if exact is not None:
            return exact
    return _nearest(records, timestamp, 0.12)


def _derive_object_mask(
    root: Path,
    frame: dict[str, Any],
    record: dict[str, Any],
    image_size: tuple[int, int],
    destination: Path,
) -> bool:
    depth_relative = frame.get("depth")
    support = record.get("depth_support")
    if not isinstance(depth_relative, str) or not isinstance(support, dict):
        return False
    bbox = support.get("depth_bbox_px")
    band = support.get("depth_band_meters")
    if not isinstance(bbox, dict) or not isinstance(band, dict):
        return False
    depth = np.load(root / depth_relative, allow_pickle=False)
    if depth.ndim != 2:
        return False
    height, width = depth.shape
    x0 = max(0, min(width, int(bbox.get("x0", 0))))
    x1 = max(x0, min(width, int(bbox.get("x1", width))))
    y0 = max(0, min(height, int(bbox.get("y0", 0))))
    y1 = max(y0, min(height, int(bbox.get("y1", height))))
    minimum = float(band.get("min", 0.0))
    maximum = float(band.get("max", 0.0))
    if x1 <= x0 or y1 <= y0 or maximum <= minimum:
        return False
    region = depth[y0:y1, x0:x1]
    selected = np.isfinite(region) & (region >= minimum) & (region <= maximum)
    confidence_relative = frame.get("confidence")
    if isinstance(confidence_relative, str) and (root / confidence_relative).exists():
        confidence = np.load(root / confidence_relative, allow_pickle=False)
        if confidence.shape == depth.shape:
            selected &= confidence[y0:y1, x0:x1] >= 1
    if not np.any(selected):
        return False
    mask = np.zeros((height, width), dtype=np.uint8)
    mask[y0:y1, x0:x1] = selected.astype(np.uint8) * 255
    destination.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(mask).resize(image_size, Image.Resampling.NEAREST).save(destination)
    return True


def _duplicate(candidate: Candidate, prior: list[Candidate], tolerance: float) -> bool:
    if candidate.timestamp is None:
        return False
    return any(
        item.timestamp is not None
        and item.timestamp_domain == candidate.timestamp_domain
        and abs(item.timestamp - candidate.timestamp) <= tolerance
        for item in prior
    )


def _copy(path: Path, destination: Path) -> None:
    if not path.exists():
        raise FileNotFoundError(f"capture asset missing: {path}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, destination)


def _write_valid_mask(
    output_dir: Path,
    prepared: dict[str, Any],
    image_size: tuple[int, int],
    index: int,
    subject_required: bool,
) -> dict[str, Any] | None:
    valid = np.ones((image_size[1], image_size[0]), dtype=bool)
    sources: list[str] = []
    resized_sources: list[dict[str, Any]] = []
    person_relative = prepared.get("person_mask")
    if isinstance(person_relative, str):
        with Image.open(output_dir / person_relative) as image:
            if image.size != image_size:
                resized_sources.append({"source": "person", "from": list(image.size), "to": list(image_size)})
            person = np.asarray(image.convert("L").resize(image_size, Image.Resampling.NEAREST)) >= 128
        valid &= ~person
        sources.append("inverse_person")
    object_relative = prepared.get("object_mask")
    if subject_required and not isinstance(object_relative, str):
        return None
    if subject_required:
        with Image.open(output_dir / object_relative) as image:
            if image.size != image_size:
                resized_sources.append({"source": "object", "from": list(image.size), "to": list(image_size)})
            subject = np.asarray(image.convert("L").resize(image_size, Image.Resampling.NEAREST)) >= 128
        valid &= subject
        sources.append("object_support")
    if not sources:
        sources.append("full_frame_static_default")
    relative = Path("masks/valid") / f"{Path(str(prepared['rgb'])).name}.png"
    destination = output_dir / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(valid.astype(np.uint8) * 255).save(destination)
    prepared["valid_mask"] = relative.as_posix()
    return {
        "frame": index,
        "path": relative.as_posix(),
        "sources": sources,
        "resized_sources": resized_sources,
        "valid_fraction": float(np.mean(valid)),
    }


def _write_frames(
    candidates: list[Candidate],
    output_dir: Path,
    person_records: list[dict[str, Any]],
    object_records: list[dict[str, Any]],
    video_records: list[dict[str, Any]],
    object_masks: bool,
) -> tuple[list[dict[str, Any]], dict[str, int], list[dict[str, Any]]]:
    frames: list[dict[str, Any]] = []
    counts = {"depth": 0, "confidence": 0, "person_mask": 0, "object_mask": 0, "valid_mask": 0}
    mask_records: list[dict[str, Any]] = []
    for index, candidate in enumerate(candidates, start=1):
        raw = candidate.frame
        source_image = candidate.root / str(raw["rgb"])
        suffix = source_image.suffix.lower() if source_image.suffix.lower() in IMAGE_SUFFIXES else ".jpg"
        image_relative = Path("images") / f"{index:06d}{suffix}"
        _copy(source_image, output_dir / image_relative)
        with Image.open(source_image) as image:
            image_size = image.size
        prepared = deepcopy(raw)
        prepared.update({
            "rgb": image_relative.as_posix(),
            "accepted": True,
            "source_kind": candidate.source_kind,
            "source_frame_index": candidate.source_index,
            "timestamp_domain": candidate.timestamp_domain,
        })
        video_record = _nearest(video_records, candidate.timestamp, 0.12)
        indexed_photometric = photometric_from_entry(video_record or {})
        existing_photometric = prepared.get("photometric") if isinstance(prepared.get("photometric"), dict) else {}
        prepared["photometric"] = {**indexed_photometric, **existing_photometric}
        if prepared.get("tracking_state") is None and video_record is not None:
            prepared["tracking_state"] = video_record.get("tracking_state")
        for key in ("depth", "confidence"):
            relative = raw.get(key)
            if not isinstance(relative, str):
                continue
            source = candidate.root / relative
            destination_relative = Path(key) / f"{index:06d}{source.suffix}"
            _copy(source, output_dir / destination_relative)
            prepared[key] = destination_relative.as_posix()
            counts[key] += 1
        person_relative = raw.get("person_mask")
        if not isinstance(person_relative, str) and candidate.source_kind == "accepted_rgbd":
            record = _nearest(person_records, candidate.timestamp, 0.12)
            person_relative = record.get("path") if record is not None else None
        if isinstance(person_relative, str) and (candidate.root / person_relative).exists():
            destination_relative = Path("masks/person") / f"{index:06d}.png"
            _copy(candidate.root / person_relative, output_dir / destination_relative)
            prepared["person_mask"] = destination_relative.as_posix()
            counts["person_mask"] += 1
        else:
            prepared.pop("person_mask", None)
        if object_masks and candidate.source_kind == "accepted_rgbd":
            record = _object_record(object_records, raw, candidate.timestamp)
            destination_relative = Path("masks/object") / f"{index:06d}.png"
            if record is not None and _derive_object_mask(
                candidate.root, raw, record, image_size, output_dir / destination_relative
            ):
                prepared["object_mask"] = destination_relative.as_posix()
                counts["object_mask"] += 1
        valid_record = _write_valid_mask(output_dir, prepared, image_size, index, object_masks)
        if valid_record is not None:
            mask_records.append(valid_record)
            counts["valid_mask"] += 1
        frames.append(prepared)
    return frames, counts, mask_records


def _finalization_status(capture_dir: Path, capture: dict[str, Any]) -> tuple[str, list[str]]:
    relative = str(capture.get("finalization_report_file", "metadata/finalization_report.json"))
    path = capture_dir / relative
    if not path.exists():
        return "missing", ["finalization_report_missing"]
    report = load_json_strict(path)
    if report.get("manifest_written") is not True:
        return "failed", ["capture_manifest_not_finalized"]
    status = str(report.get("status", "unknown"))
    warnings = [] if status == "finalized" else [f"finalization_status_{status}"]
    return status, warnings


def prepare_capture(
    capture_dir: Path,
    out_dir: Path,
    recipe: str = "auto",
    target_frames: int | None = None,
    max_edge: int = 1920,
    dedup_tolerance_seconds: float = 0.08,
) -> dict[str, Any]:
    capture_dir = capture_dir.resolve()
    out_dir = out_dir.resolve()
    capture = load_capture(capture_dir)
    _require_empty_output(out_dir)
    plan = plan_reconstruction(capture_dir, out_dir / "plan", recipe=recipe)
    quality = run_capture_quality_report(capture_dir, out_dir / "capture_quality")
    recipe_name, recipe_source = resolve_recipe(capture, recipe)
    recipe_config = deepcopy(RECIPES[recipe_name])
    subject_masks_required = recipe_name == "object"
    target = max(1, min(int(target_frames or recipe_config["target_frames"]), 600))
    accepted = _candidates(capture_dir, capture, "accepted_rgbd")
    if not accepted:
        raise ValueError("capture has no accepted RGB-D frames")
    warnings: list[str] = []
    finalization_status, finalization_warnings = _finalization_status(capture_dir, capture)
    warnings.extend(finalization_warnings)
    supplements: list[Candidate] = []
    extraction_summary: dict[str, Any] | None = None
    person_records = _load_json_lines(capture_dir / str(capture.get(
        "person_mask_index_file", "metadata/person_mask_index.jsonl"
    )))
    object_records = _object_records(capture_dir, capture)
    video = capture_dir / str(capture.get("video_file", "video/capture.mov"))
    frame_index = capture_dir / str(capture.get("frame_index_file", "metadata/frame_index.jsonl"))
    video_records = _load_json_lines(frame_index)
    with tempfile.TemporaryDirectory(prefix="capture_splat_prepare_") as temporary:
        if len(accepted) < target and video.exists() and frame_index.exists():
            extracted_dir = Path(temporary) / "video_frames"
            extraction_summary = run_extract_frames(
                video,
                extracted_dir,
                target_frames=min(600, target + len(accepted)),
                max_edge=max_edge,
                pick="sharpest",
                frame_index=frame_index,
            )
            if (extracted_dir / "capture.json").exists():
                extracted_capture = load_capture(extracted_dir)
                extracted = _candidates(extracted_dir, extracted_capture, "continuous_video")
                if any(item.timestamp_domain != "ar_session" for item in extracted):
                    warnings.append("video_index_missing_ar_timestamp_cross_source_dedup_unavailable")
                for item in extracted:
                    if len(accepted) + len(supplements) >= target:
                        break
                    if not _duplicate(item, accepted + supplements, dedup_tolerance_seconds):
                        supplements.append(item)
            else:
                warnings.append("video_frame_pose_attachment_empty")
        elif len(accepted) < target:
            warnings.append("continuous_video_or_frame_index_missing")
        merged = accepted + supplements
        merged.sort(key=lambda item: (
            item.timestamp is None,
            item.timestamp if item.timestamp is not None else float(item.source_index),
            item.source_kind,
        ))
        frames_dir = out_dir / "frames"
        prepared_frames, copied, mask_records = _write_frames(
            merged[:target],
            frames_dir,
            person_records,
            object_records,
            video_records,
            subject_masks_required,
        )
    prepared_manifest = {
        "schema": "capture_splat.v0.3",
        "source": "capture_splat.prepare_capture",
        "capture_mode": capture.get("capture_mode"),
        "capture_profile": capture.get("capture_profile"),
        "capture_intent": capture.get("capture_intent"),
        "session_config": capture.get("session_config"),
        "frames": prepared_frames,
        "preparation": {
            "recipe": recipe_name,
            "target_frames": target,
            "accepted_rgbd_precedence": True,
            "dedup_tolerance_seconds": dedup_tolerance_seconds,
            "originals_preserved": True,
        },
        "authority": {
            "device_poses_are_priors": True,
            "derived_masks_are_proposals": True,
            "quality_claim": False,
        },
    }
    write_json_strict(out_dir / "frames/capture.json", prepared_manifest)
    frame_evidence = load_frame_evidence(out_dir / "frames/capture.json")
    camera_report = camera_evidence_report(out_dir / "frames/images", frame_evidence)
    photometric_report = photometric_evidence_report(prepared_frames)
    object_masks_required = subject_masks_required
    missing_valid_masks = [
        Path(str(frame["rgb"])).name for frame in prepared_frames if not isinstance(frame.get("valid_mask"), str)
    ]
    mask_report = {
        "schema": "capture_splat.valid_mask_report.v0.1",
        "semantics": "white_valid_for_features_and_training",
        "records": mask_records,
        "frames_with_masks": len(mask_records),
        "required_for_recipe": object_masks_required,
        "missing_frames": missing_valid_masks,
        "decision": "hold" if object_masks_required and missing_valid_masks else "ready",
        "authority": {"derived_masks_are_proposals": True, "quality_claim": False},
    }
    if object_masks_required and missing_valid_masks:
        warnings.append("object_support_masks_incomplete")
    metadata_dir = out_dir / "frames/metadata"
    write_json_strict(metadata_dir / "camera_evidence.json", camera_report)
    write_json_strict(metadata_dir / "photometric_evidence.json", photometric_report)
    write_json_strict(metadata_dir / "valid_mask_report.json", mask_report)
    actual_count = len(prepared_frames)
    matcher = "retrieval" if actual_count > 250 else "exhaustive"
    sfm_request = {
        "images": "frames/images",
        "method": "global",
        "camera_policy": "auto",
        "view_graph_calibration": "auto",
        "masks": "auto",
        "post_ba_backend": "none",
        "features": "hloc" if matcher == "retrieval" else "sift",
        "matcher": matcher,
        "background_sphere": bool(recipe_config["background_sphere"]),
    }
    if quality["decision"] == "reject" or finalization_status == "failed":
        decision = "reject"
    elif warnings or quality["decision"] == "hold" or actual_count < target:
        decision = "hold"
    else:
        decision = "ready"
    summary = {
        "schema": SUMMARY_SCHEMA,
        "capture_dir": str(capture_dir),
        "output_dir": str(out_dir),
        "recipe": {"name": recipe_name, "source": recipe_source},
        "target_frames": target,
        "accepted_rgbd_frames": len(accepted),
        "continuous_video_supplements": len(supplements),
        "prepared_frames": actual_count,
        "copied_sidecars": copied,
        "camera_evidence": camera_report,
        "photometric_evidence": photometric_report,
        "valid_masks": mask_report,
        "finalization_status": finalization_status,
        "quality_decision": quality["decision"],
        "plan_decision": plan["decision"],
        "warnings": sorted(set(warnings)),
        "sfm_request": sfm_request,
        "extraction_summary": extraction_summary,
        "decision": decision,
        "authority": {
            "frame_preparation_evidence": True,
            "capture_quality_proxy_only": True,
            "derived_masks_are_proposals": True,
            "quality_claim": False,
        },
    }
    write_json_strict(out_dir / "capture_splat_prepare_summary.json", summary)
    return summary
