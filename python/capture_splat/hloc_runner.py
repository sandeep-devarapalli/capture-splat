from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from typing import Any

from .sfm_evidence import apply_camera_priors, filter_hloc_features_by_masks, load_frame_evidence

RETRIEVAL_CONFIG = "netvlad"
FEATURE_CONFIG = "aliked-n16"
MATCHER_CONFIG = "aliked+lightglue"
PAIRS_FILENAME = f"pairs-{RETRIEVAL_CONFIG}.txt"


def hloc_status() -> dict[str, Any]:
    hloc_present = importlib.util.find_spec("hloc") is not None
    pycolmap_present = importlib.util.find_spec("pycolmap") is not None
    return {
        "ready": hloc_present and pycolmap_present,
        "hloc_importable": hloc_present,
        "pycolmap_importable": pycolmap_present,
        "retrieval_config": RETRIEVAL_CONFIG,
        "feature_config": FEATURE_CONFIG,
        "matcher_config": MATCHER_CONFIG,
    }


def planned_frontend(images_dir: Path, out_dir: Path, database: Path, top_k: int) -> list[list[str]]:
    hloc_dir = out_dir / "hloc"
    pairs = hloc_dir / PAIRS_FILENAME
    return [
        ["python-hloc", "extract", RETRIEVAL_CONFIG, str(images_dir), str(hloc_dir)],
        ["python-hloc", "pairs", str(pairs), "--top-k", str(int(top_k))],
        ["python-hloc", "extract", FEATURE_CONFIG, str(images_dir), str(hloc_dir)],
        ["python-hloc", "match", MATCHER_CONFIG, str(pairs), str(hloc_dir)],
        [
            "colmap", "matches_importer",
            "--database_path", str(database),
            "--match_list_path", str(pairs),
            "--TwoViewGeometry.min_inlier_ratio", "0.1",
            "--TwoViewGeometry.max_num_trials", "20000",
        ],
    ]


def run_hloc_frontend(
    images_dir: Path,
    out_dir: Path,
    database: Path,
    top_k: int = 32,
    camera_policy: str = "single",
    capture_manifest: Path | None = None,
    mask_dir: Path | None = None,
    masks: str = "auto",
) -> dict[str, Any]:
    status = hloc_status()
    if not status["ready"]:
        raise RuntimeError("hloc_missing")
    from hloc import extract_features, match_features, pairs_from_retrieval, reconstruction
    import pycolmap

    images_dir = images_dir.resolve()
    out_dir = out_dir.resolve()
    database = database.resolve()
    hloc_dir = out_dir / "hloc"
    hloc_dir.mkdir(parents=True, exist_ok=True)
    pairs = hloc_dir / PAIRS_FILENAME
    retrieval_conf = extract_features.confs[RETRIEVAL_CONFIG]
    feature_conf = extract_features.confs[FEATURE_CONFIG]
    matcher_conf = match_features.confs[MATCHER_CONFIG]

    retrieval_path = extract_features.main(retrieval_conf, images_dir, hloc_dir)
    pairs_from_retrieval.main(retrieval_path, pairs, num_matched=int(top_k))
    feature_path = extract_features.main(feature_conf, images_dir, hloc_dir)
    mask_report = None
    if mask_dir is not None and mask_dir.is_dir():
        mask_report = filter_hloc_features_by_masks(feature_path, mask_dir)
        if masks == "required" and (mask_report["missing_masks"] or mask_report["dimension_mismatches"]):
            raise RuntimeError("required HLOC masks are missing or dimension-mismatched")
    elif masks == "required":
        raise RuntimeError("required HLOC mask directory is missing")
    match_path = match_features.main(matcher_conf, pairs, feature_conf["output"], hloc_dir)

    reconstruction.create_empty_db(database)
    camera_mode = pycolmap.CameraMode.PER_IMAGE if camera_policy == "per-frame" else pycolmap.CameraMode.SINGLE
    pycolmap.import_images(str(database), str(images_dir), camera_mode)
    camera_report = None
    if camera_policy == "per-frame":
        camera_report = apply_camera_priors(database, images_dir, load_frame_evidence(capture_manifest))
    image_ids = reconstruction.get_image_ids(database)
    with pycolmap.Database.open(database) as database_handle:
        reconstruction.import_features(image_ids, database_handle, feature_path)
        reconstruction.import_matches(image_ids, database_handle, pairs, match_path, None, False)
    verification = planned_frontend(images_dir, out_dir, database, top_k)[-1]
    completed = subprocess.run(verification, text=True)
    if completed.returncode != 0:
        raise RuntimeError(f"HLOC geometric verification failed with exit {completed.returncode}")
    return {
        "retrieval_config": RETRIEVAL_CONFIG,
        "feature_config": FEATURE_CONFIG,
        "matcher_config": MATCHER_CONFIG,
        "retrieval_top_k": int(top_k),
        "pairs": str(pairs),
        "retrieval_features": str(retrieval_path),
        "local_features": str(feature_path),
        "matches": str(match_path),
        "database": str(database),
        "geometric_verification_command": verification,
        "camera_mode": camera_policy,
        "camera_evidence": camera_report,
        "mask_filter": mask_report,
    }
