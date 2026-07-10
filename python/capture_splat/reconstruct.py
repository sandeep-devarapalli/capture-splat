from __future__ import annotations

from copy import deepcopy
import hashlib
from pathlib import Path
from typing import Any

from .capture_schema import load_capture
from .gsplat_ladder import run_gsplat_ladder
from .json_utils import load_json_strict, write_json_strict
from .ply_stats import prune_ply_by_alpha
from .prepare_capture import prepare_capture
from .reconstruction_recipe import RECIPES, resolve_recipe
from .render_source_qa import run_render_source_qa
from .rgbd_seed import build_rgbd_metric_seed
from .sfm_runner import run_sfm
from .vksplat_ladder import DEFAULT_STEPS, run_vksplat_ladder
from .world_studio_export import export_world_studio_handoff

SUMMARY_NAME = "capture_splat_reconstruction_summary.json"
SUMMARY_SCHEMA = "capture_splat.reconstruction_summary.v0.1"
STAGES = ("prepare", "sfm", "seed", "train", "prune", "qa", "export")
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp"}
STAGE_CONFIG_KEYS = {
    "prepare": ("capture_manifest", "capture_manifest_checksum", "recipe"),
    "sfm": ("allow_cpu_matching", "retrieval_top_k"),
    "train": ("backend", "backend_root", "steps", "stop_reset_at"),
    "prune": ("prune_alpha", "max_pruned_fraction"),
    "qa": (
        "qa_render_dir",
        "qa_pairs_json",
        "qa_pairs_checksum",
        "qa_provenance_json",
        "qa_provenance_checksum",
    ),
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _render_set_digest(root: Path) -> tuple[int, str]:
    files = sorted(
        path for path in root.rglob("*")
        if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES
    )
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(_sha256(path).encode("ascii"))
        digest.update(b"\0")
    return len(files), f"sha256:{digest.hexdigest()}"


def _load_resume(
    path: Path,
    resume: bool,
    required: tuple[str, ...],
    expected_schema: str,
) -> dict[str, Any] | None:
    if not resume or not path.exists():
        return None
    summary = load_json_strict(path)
    if summary.get("schema") != expected_schema:
        raise ValueError(f"resume summary {path} has unexpected schema {summary.get('schema')!r}")
    missing = [key for key in required if key not in summary]
    if missing:
        raise ValueError(f"resume summary {path} is missing keys: {', '.join(missing)}")
    return summary


def _selected_splat(ladder: dict[str, Any]) -> tuple[Path | None, dict[str, Any] | None]:
    candidates = []
    for rung in ladder.get("rungs", []):
        path = rung.get("splat_ply")
        candidate = Path(path).resolve() if isinstance(path, str) else None
        if (
            candidate is not None
            and candidate.exists()
            and rung.get("finite_ply") is True
            and rung.get("decision") != "reject"
        ):
            candidates.append((rung.get("decision") == "promote", int(rung.get("step", 0)), candidate, rung))
    if not candidates:
        return None, None
    _, _, path, rung = max(candidates, key=lambda item: (item[0], item[1]))
    return path, rung


def _training_hold_is_qa_only(rung: dict[str, Any]) -> bool:
    reasons = set(rung.get("reasons", []))
    return bool(reasons) and reasons <= {
        "finite_output_without_render_source_qa",
        "no_quality_gate_promoted",
    }


def _promotion_blockers(
    prepared: dict[str, Any],
    sfm: dict[str, Any],
    selected_rung: dict[str, Any],
    prune_decision: str,
    qa_decision: str,
) -> list[str]:
    blockers = []
    if prepared.get("decision") != "ready":
        blockers.append("capture_preparation_not_ready")
    if sfm.get("decision") != "promote":
        blockers.append("sfm_not_promoted")
    if selected_rung.get("decision") != "promote" and not _training_hold_is_qa_only(selected_rung):
        blockers.append("training_rung_not_promoted")
    if prune_decision != "pruned":
        blockers.append("pruning_not_validated")
    if qa_decision != "promote":
        blockers.append("render_source_qa_not_promoted")
    return blockers


def _qa_provenance(path: Path | None, gaussian: Path) -> tuple[bool, str, dict[str, Any] | None]:
    if path is None or not path.exists():
        return False, "render_provenance_missing", None
    try:
        provenance = load_json_strict(path)
    except (OSError, ValueError):
        return False, "render_provenance_invalid", None
    if not isinstance(provenance, dict):
        return False, "render_provenance_invalid", None
    checksum = provenance.get("gaussian_checksum")
    if checksum != _sha256(gaussian):
        return False, "render_provenance_gaussian_mismatch", provenance
    return True, "render_provenance_verified", provenance


def _file_ref_valid(ref: dict[str, Any], path_key: str, root: Path) -> bool:
    relative = ref.get(path_key)
    checksum = ref.get("checksum")
    if not isinstance(relative, str) or not isinstance(checksum, str):
        return False
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        return False
    if isinstance(ref.get("size_bytes"), int) and path.stat().st_size != ref["size_bytes"]:
        return False
    return _sha256(path) == checksum


def _asset_refs_valid(value: Any, root: Path) -> bool:
    if isinstance(value, list):
        return all(_asset_refs_valid(item, root) for item in value)
    if not isinstance(value, dict):
        return True
    if "path" in value or "checksum" in value:
        return _file_ref_valid(value, "path", root)
    return all(_asset_refs_valid(item, root) for item in value.values())


def _handoff_assets_valid(handoff: dict[str, Any], gaussian: Path, handoff_dir: Path) -> bool:
    assets = handoff.get("assets")
    frames = handoff.get("source_frames")
    if not isinstance(assets, dict) or not isinstance(frames, list) or not frames:
        return False
    if not _asset_refs_valid(assets, handoff_dir):
        return False
    if not all(isinstance(frame, dict) and _file_ref_valid(frame, "rgb_path", handoff_dir) for frame in frames):
        return False
    gaussian_ref = assets.get("gaussian_ply") or assets.get("gaussian")
    if not isinstance(gaussian_ref, dict):
        return False
    expected = _sha256(gaussian)
    return gaussian_ref.get("checksum") == expected


def _completed_stage_records(summary: dict[str, Any]) -> dict[str, dict[str, Any]]:
    records = {}
    for stage in summary.get("stages", []):
        if not isinstance(stage, dict) or stage.get("name") not in STAGES:
            continue
        if stage.get("decision") not in {"blocked", "reject", "skipped"}:
            records[str(stage["name"])] = stage
    return records


def _failed_stage_records(summary: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(stage["name"]): stage
        for stage in summary.get("stages", [])
        if isinstance(stage, dict)
        and stage.get("name") in STAGES
        and stage.get("decision") in {"blocked", "reject"}
    }


def _resume_config_mismatches(
    prior: dict[str, Any],
    current: dict[str, Any],
    completed: dict[str, dict[str, Any]],
) -> list[str]:
    previous = prior.get("run_config")
    if not isinstance(previous, dict):
        return ["run_config_missing"]
    mismatches = []
    for stage, record in completed.items():
        for key in STAGE_CONFIG_KEYS.get(stage, ()):
            if (
                stage == "qa"
                and record.get("provenance_verified") is not True
                and key in {"qa_provenance_json", "qa_provenance_checksum"}
            ):
                continue
            if previous.get(key) != current.get(key):
                mismatches.append(f"{stage}:{key}")
    return sorted(set(mismatches))


def _backend_ready(backend: str, root: Path | None) -> bool:
    if root is None or not root.is_dir():
        return False
    candidates = (
        (root / "simple_trainer.py", root / "vksplat/simple_trainer.py")
        if backend == "vksplat"
        else (root / "examples/simple_trainer.py", root / "simple_trainer.py")
    )
    return any(path.is_file() for path in candidates)


def _stage(name: str, decision: str, summary_path: Path | None = None, **values: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"name": name, "decision": decision}
    if summary_path is not None:
        result["summary"] = str(summary_path.resolve())
    result.update(values)
    return result


def _write(out_dir: Path, summary: dict[str, Any]) -> None:
    write_json_strict(out_dir / SUMMARY_NAME, summary)


def _dry_run(
    capture_dir: Path,
    out_dir: Path,
    backend: str,
    backend_root: Path | None,
    recipe: str,
    steps: list[int],
    stop_after: str,
    qa_render_dir: Path | None,
) -> dict[str, Any]:
    capture = load_capture(capture_dir)
    recipe_name, recipe_source = resolve_recipe(capture, recipe)
    config = deepcopy(RECIPES[recipe_name])
    training_blocked = not _backend_ready(backend, backend_root)
    planned = [
        _stage("prepare", "planned", output=str((out_dir / "01_prepare").resolve())),
        _stage("sfm", "planned", route="from_prepare_summary", method="glomap"),
        _stage("seed", "planned", policy="arkit_rgbd_if_sim3_gate_passes"),
        _stage(
            "train",
            "blocked" if training_blocked else "planned",
            backend=backend,
            backend_root=str(backend_root.resolve()) if backend_root else None,
            steps=steps,
        ),
        _stage("prune", "blocked" if training_blocked else "planned", authority="viewer_hygiene_only"),
        _stage(
            "qa",
            "blocked" if training_blocked else ("planned" if qa_render_dir else "skipped"),
            render_dir=str(qa_render_dir.resolve()) if qa_render_dir else None,
        ),
        _stage("export", "blocked" if training_blocked else "planned", status="visual_evidence_with_3dgs_proposal"),
    ]
    planned = planned[: STAGES.index(stop_after) + 1]
    summary = {
        "schema": SUMMARY_SCHEMA,
        "capture_dir": str(capture_dir),
        "output_dir": str(out_dir),
        "backend": backend,
        "recipe": {"name": recipe_name, "source": recipe_source, **config},
        "steps": steps,
        "resume": False,
        "dry_run": True,
        "stop_after": stop_after,
        "stages": planned,
        "decision": "dry_run",
        "authority": {"execution_plan_only": True, "quality_claim": False},
    }
    _write(out_dir, summary)
    return summary


def reconstruct_capture(
    capture_dir: Path,
    out_dir: Path,
    backend: str = "vksplat",
    backend_root: Path | None = None,
    recipe: str = "auto",
    steps: list[int] | None = None,
    qa_render_dir: Path | None = None,
    qa_pairs_json: Path | None = None,
    qa_provenance_json: Path | None = None,
    resume: bool = False,
    dry_run: bool = False,
    stop_after: str = "export",
    allow_cpu_matching: bool = False,
    retrieval_top_k: int = 32,
    prune_alpha: float = 12.0,
    max_pruned_fraction: float = 0.6,
    stop_reset_at: int | None = None,
) -> dict[str, Any]:
    capture_dir = capture_dir.resolve()
    out_dir = out_dir.resolve()
    backend_root = backend_root.resolve() if backend_root else None
    qa_render_dir = qa_render_dir.resolve() if qa_render_dir else None
    qa_pairs_json = qa_pairs_json.resolve() if qa_pairs_json else None
    qa_provenance_json = qa_provenance_json.resolve() if qa_provenance_json else None
    if qa_render_dir is not None and qa_provenance_json is None:
        qa_provenance_json = qa_render_dir / "capture_splat_render_provenance.json"
    step_values = list(steps or DEFAULT_STEPS)
    if backend not in {"vksplat", "gsplat"}:
        raise ValueError("backend must be vksplat or gsplat")
    if recipe != "auto" and recipe not in RECIPES:
        raise ValueError(f"unknown reconstruction recipe: {recipe}")
    if stop_after not in STAGES:
        raise ValueError(f"stop-after must be one of {', '.join(STAGES)}")
    if not step_values or any(step <= 0 for step in step_values):
        raise ValueError("training steps must be positive")
    if resume and dry_run:
        raise ValueError("resume and dry-run cannot be combined")
    run_config = {
        "capture_manifest": str((capture_dir / "capture.json").resolve()),
        "capture_manifest_checksum": _sha256(capture_dir / "capture.json"),
        "backend": backend,
        "backend_root": str(backend_root) if backend_root else None,
        "recipe": recipe,
        "steps": step_values,
        "qa_render_dir": str(qa_render_dir) if qa_render_dir else None,
        "qa_pairs_json": str(qa_pairs_json) if qa_pairs_json else None,
        "qa_pairs_checksum": _sha256(qa_pairs_json) if qa_pairs_json else None,
        "qa_provenance_json": str(qa_provenance_json) if qa_provenance_json else None,
        "qa_provenance_checksum": (
            _sha256(qa_provenance_json)
            if qa_provenance_json is not None and qa_provenance_json.exists()
            else None
        ),
        "allow_cpu_matching": allow_cpu_matching,
        "retrieval_top_k": retrieval_top_k,
        "prune_alpha": prune_alpha,
        "max_pruned_fraction": max_pruned_fraction,
        "stop_reset_at": stop_reset_at,
    }
    if out_dir.exists() and any(out_dir.iterdir()) and not resume:
        raise FileExistsError(f"reconstruction output is not empty: {out_dir}")
    out_dir.mkdir(parents=True, exist_ok=True)
    if dry_run:
        return _dry_run(
            capture_dir, out_dir, backend, backend_root, recipe, step_values, stop_after, qa_render_dir
        )
    prior: dict[str, Any] = {}
    completed_stages: dict[str, dict[str, Any]] = {}
    if resume:
        prior_path = out_dir / SUMMARY_NAME
        if not prior_path.exists():
            raise FileNotFoundError(f"resume requires {prior_path}")
        prior = load_json_strict(prior_path)
        if prior.get("schema") != SUMMARY_SCHEMA:
            raise ValueError("resume reconstruction summary has an unexpected schema")
        completed_stages = _completed_stage_records(prior)
        failed_stages = _failed_stage_records(prior)
        retry_blockers = [
            name for name, stage in failed_stages.items()
            if not (name == "train" and stage.get("decision") == "blocked" and not stage.get("summary"))
        ]
        if retry_blockers:
            raise ValueError(
                "resume cannot retry rejected or partially written stages in place; "
                f"use a new output directory after fixing: {', '.join(sorted(retry_blockers))}"
            )
        mismatches = _resume_config_mismatches(prior, run_config, completed_stages)
        if mismatches:
            raise ValueError(f"resume configuration changed completed stages: {', '.join(mismatches)}")

    summary: dict[str, Any] = {
        "schema": SUMMARY_SCHEMA,
        "capture_dir": str(capture_dir),
        "output_dir": str(out_dir),
        "backend": backend,
        "backend_root": str(backend_root) if backend_root else None,
        "recipe": recipe,
        "steps": step_values,
        "resume": resume,
        "dry_run": False,
        "stop_after": stop_after,
        "run_config": run_config,
        "stages": [],
        "decision": "hold",
        "authority": {
            "source_frames_are_visual_evidence": True,
            "trained_splats_are_review_proposals": True,
            "quality_claim": False,
        },
    }

    def record(stage: dict[str, Any]) -> None:
        summary["stages"].append(stage)
        _write(out_dir, summary)

    prepare_dir = out_dir / "01_prepare"
    prepare_path = prepare_dir / "capture_splat_prepare_summary.json"
    prepared = _load_resume(
        prepare_path,
        "prepare" in completed_stages,
        ("decision", "sfm_request"),
        "capture_splat.prepare_capture_summary.v0.1",
    )
    prepare_resumed = prepared is not None
    try:
        prepared = prepared or prepare_capture(capture_dir, prepare_dir, recipe=recipe)
    except Exception as error:
        record(_stage("prepare", "reject", error=str(error)))
        summary.update({"decision": "reject", "failed_stage": "prepare"})
        _write(out_dir, summary)
        return summary
    record(_stage("prepare", str(prepared.get("decision", "hold")), prepare_path, resumed=prepare_resumed))
    if prepared.get("decision") == "reject" or stop_after == "prepare":
        summary["decision"] = "reject" if prepared.get("decision") == "reject" else "hold"
        _write(out_dir, summary)
        return summary

    sfm_dir = out_dir / "02_sfm"
    sfm_path = sfm_dir / "capture_splat_sfm_summary.json"
    sfm = _load_resume(
        sfm_path,
        "sfm" in completed_stages,
        ("decision",),
        "capture_splat.sfm_summary.v0.1",
    )
    sfm_resumed = sfm is not None
    sfm_request = prepared["sfm_request"]
    try:
        sfm = sfm or run_sfm(
            prepare_dir / sfm_request["images"],
            sfm_dir,
            method=str(sfm_request["method"]),
            matcher=str(sfm_request["matcher"]),
            features=str(sfm_request["features"]),
            retrieval_top_k=retrieval_top_k,
            background_sphere=bool(sfm_request["background_sphere"]),
            allow_cpu_matching=allow_cpu_matching,
        )
    except Exception as error:
        failed = load_json_strict(sfm_path) if sfm_path.exists() else {}
        decision = "blocked" if failed.get("decision") == "blocked" or "blocked" in str(error) else "reject"
        record(_stage("sfm", decision, sfm_path if sfm_path.exists() else None, error=str(error)))
        summary.update({"decision": decision, "failed_stage": "sfm"})
        _write(out_dir, summary)
        return summary
    record(_stage("sfm", str(sfm.get("decision", "hold")), sfm_path, resumed=sfm_resumed))
    if sfm.get("decision") in {"reject", "blocked"} or stop_after == "sfm":
        summary["decision"] = str(sfm.get("decision", "hold"))
        _write(out_dir, summary)
        return summary

    seed_dir = out_dir / "03_rgbd_seed"
    seed_path = seed_dir / "capture_splat_rgbd_seed_summary.json"
    seed = _load_resume(
        seed_path,
        "seed" in completed_stages,
        ("decision", "package_augmented", "output_package"),
        "capture_splat.rgbd_seed_summary.v0.1",
    )
    seed_resumed = seed is not None
    try:
        seed = seed or build_rgbd_metric_seed(prepare_dir / "frames", sfm_dir, seed_dir)
    except Exception as error:
        seed = {"decision": "hold", "package_augmented": False, "error": str(error)}
    record(_stage("seed", str(seed.get("decision", "hold")), seed_path if seed_path.exists() else None, resumed=seed_resumed, package_augmented=bool(seed.get("package_augmented")), error=seed.get("error")))
    training_package = Path(seed["output_package"]) if seed.get("package_augmented") else sfm_dir
    if not training_package.exists():
        record(_stage("seed", "blocked", error=f"training package missing: {training_package}"))
        summary.update({"decision": "blocked", "failed_stage": "seed"})
        _write(out_dir, summary)
        return summary
    summary["training_package"] = str(training_package.resolve())
    if stop_after == "seed":
        _write(out_dir, summary)
        return summary

    if not _backend_ready(backend, backend_root):
        record(_stage("train", "blocked", error=f"{backend} trainer is not ready under {backend_root}"))
        summary.update({"decision": "blocked", "failed_stage": "train"})
        _write(out_dir, summary)
        return summary
    train_dir = out_dir / "04_training"
    ladder_name = f"capture_splat_{backend}_ladder_summary.json"
    ladder_path = train_dir / ladder_name
    ladder = _load_resume(
        ladder_path,
        "train" in completed_stages,
        ("decision", "rungs"),
        f"capture_splat.{backend}_ladder_summary.v0.1",
    )
    train_resumed = ladder is not None
    try:
        if ladder is None and backend == "vksplat":
            ladder = run_vksplat_ladder(
                training_package,
                train_dir,
                backend_root,
                steps=step_values,
                stop_reset_at=stop_reset_at,
            )
        elif ladder is None:
            ladder = run_gsplat_ladder(training_package, train_dir, backend_root, steps=step_values)
    except Exception as error:
        failed = load_json_strict(ladder_path) if ladder_path.exists() else {}
        record(_stage("train", str(failed.get("decision", "reject")), ladder_path if ladder_path.exists() else None, error=str(error)))
        summary.update({"decision": "reject", "failed_stage": "train"})
        _write(out_dir, summary)
        return summary
    assert ladder is not None
    candidate, selected_rung = _selected_splat(ladder)
    record(_stage("train", str(ladder.get("decision", "hold")), ladder_path, resumed=train_resumed, candidate_ply=str(candidate) if candidate else None, selected_step=selected_rung.get("step") if selected_rung else None))
    if ladder.get("decision") == "reject" or candidate is None or selected_rung is None:
        summary.update({"decision": "reject", "failed_stage": "train"})
        _write(out_dir, summary)
        return summary
    summary["raw_gaussian_ply"] = str(candidate)
    if stop_after == "train":
        _write(out_dir, summary)
        return summary

    prune_dir = out_dir / "05_prune"
    prune_dir.mkdir(parents=True, exist_ok=True)
    alpha_tag = f"{prune_alpha:g}".replace(".", "p")
    pruned = prune_dir / f"{candidate.stem}.pruned_a{alpha_tag}.ply"
    prune_path = pruned.with_suffix(pruned.suffix + ".prune_report.json")
    prune = _load_resume(
        prune_path,
        "prune" in completed_stages,
        ("decision", "output"),
        "capture_splat.ply_prune_report.v0.1",
    )
    prune_resumed = prune is not None
    try:
        prune = prune or prune_ply_by_alpha(
            candidate, pruned, min_alpha=prune_alpha, max_dropped_fraction=max_pruned_fraction
        )
        selected = Path(prune["output"])
        if not selected.is_file():
            raise FileNotFoundError(f"pruned PLY missing: {selected}")
        prune_decision = str(prune.get("decision", "pruned"))
    except Exception as error:
        existing_report = load_json_strict(prune_path) if prune_path.exists() else None
        if isinstance(existing_report, dict) and existing_report.get("decision") == "reject":
            prune = existing_report
            selected = candidate
            prune_decision = "reject"
        else:
            selected = candidate
            prune_decision = "hold"
            prune = {"error": str(error), "output": None}
    summary["selected_gaussian_ply"] = str(selected.resolve())
    record(_stage("prune", prune_decision, prune_path if prune_path.exists() else None, resumed=prune_resumed, selected_ply=str(selected.resolve()), error=prune.get("error")))
    if prune_decision == "reject":
        summary.update({"decision": "reject", "failed_stage": "prune"})
        _write(out_dir, summary)
        return summary
    if stop_after == "prune":
        _write(out_dir, summary)
        return summary

    qa_dir = out_dir / "06_render_qa"
    qa_path = qa_dir / "capture_splat_render_source_qa_summary.json"
    render_count = 0
    render_digest = None
    source_count = 0
    source_digest = None
    if qa_render_dir is not None and qa_render_dir.exists():
        render_count, render_digest = _render_set_digest(qa_render_dir)
        source_count, source_digest = _render_set_digest(training_package / "images")
    prior_qa_stage = completed_stages.get("qa")
    reuse_qa = (
        prior_qa_stage is not None
        and prior_qa_stage.get("render_set_digest") == render_digest
        and prior_qa_stage.get("render_file_count") == render_count
        and prior_qa_stage.get("source_set_digest") == source_digest
        and prior_qa_stage.get("source_file_count") == source_count
    )
    qa = _load_resume(
        qa_path,
        reuse_qa,
        ("decision",),
        "capture_splat.render_source_qa.v0.1",
    )
    qa_resumed = qa is not None
    if qa_render_dir is None:
        qa = {"decision": "skipped", "reason": "raw_render_directory_not_supplied"}
    else:
        try:
            qa = qa or run_render_source_qa(
                training_package / "images", qa_render_dir, qa_dir, pairs_json=qa_pairs_json
            )
        except Exception as error:
            qa = {"decision": "reject", "error": str(error)}
    qa_decision = str(qa.get("decision", "hold"))
    provenance_verified = False
    provenance_reason = "raw_render_directory_not_supplied"
    if qa_render_dir is not None and qa_decision != "reject":
        provenance_verified, provenance_reason, _ = _qa_provenance(qa_provenance_json, selected)
        if not provenance_verified:
            qa_decision = "hold"
    record(_stage("qa", qa_decision, qa_path if qa_path.exists() else None, resumed=qa_resumed, metrics_decision=qa.get("decision"), source_file_count=source_count, source_set_digest=source_digest, render_file_count=render_count, render_set_digest=render_digest, provenance_verified=provenance_verified, provenance_reason=provenance_reason, gaussian_checksum=_sha256(selected), error=qa.get("error"), reason=qa.get("reason")))
    if qa.get("decision") == "reject":
        summary.update({"decision": "reject", "failed_stage": "qa"})
        _write(out_dir, summary)
        return summary
    if stop_after == "qa":
        blockers = _promotion_blockers(prepared, sfm, selected_rung, prune_decision, qa_decision)
        summary["promotion_blockers"] = blockers
        summary["decision"] = "promote" if not blockers else "hold"
        _write(out_dir, summary)
        return summary

    export_dir = out_dir / "07_world_studio"
    manifest_path = export_dir / "capture-splat.world-studio.json"
    handoff = _load_resume(
        manifest_path,
        "export" in completed_stages,
        ("status", "assets"),
        "capture_splat.world_studio_handoff.v0.2",
    )
    export_resumed = handoff is not None
    try:
        if handoff is not None and not _handoff_assets_valid(handoff, selected, export_dir):
            raise ValueError("resumed World Studio handoff assets are missing, corrupted, or stale")
        handoff = handoff or export_world_studio_handoff(
            training_package,
            export_dir,
            gaussian=selected,
            capture_manifest=prepare_dir / "frames/capture.json",
            copy_files=True,
        )
    except Exception as error:
        record(_stage("export", "blocked", error=str(error)))
        summary.update({"decision": "blocked", "failed_stage": "export"})
        _write(out_dir, summary)
        return summary
    record(_stage("export", "ready", manifest_path, resumed=export_resumed))
    summary["world_studio_manifest"] = str(manifest_path.resolve())
    blockers = _promotion_blockers(prepared, sfm, selected_rung, prune_decision, qa_decision)
    summary["promotion_blockers"] = blockers
    summary["decision"] = "promote" if not blockers else "hold"
    _write(out_dir, summary)
    return summary
