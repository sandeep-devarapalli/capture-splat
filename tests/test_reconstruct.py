from pathlib import Path

from capture_splat.json_utils import load_json_strict, write_json_strict
from capture_splat.reconstruct import (
    _handoff_assets_valid,
    _promotion_blockers,
    _qa_provenance,
    _render_set_digest,
    _resume_config_mismatches,
    _selected_splat,
    _sha256,
    reconstruct_capture,
)


def _capture(root: Path) -> Path:
    root.mkdir(parents=True)
    write_json_strict(root / "capture.json", {
        "schema": "capture_splat.v0.3",
        "capture_intent": "scene_cluster",
        "frames": [{
            "rgb": "rgb/frame.jpg",
            "timestamp": 0.0,
            "transform_matrix": [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]],
            "intrinsics": {"fl_x": 100, "fl_y": 100, "cx": 50, "cy": 40, "w": 100, "h": 80},
            "capture_quality": {"accepted": True},
        }],
    })
    return root


def test_reconstruct_dry_run_writes_explicit_blockers_and_skips(tmp_path: Path) -> None:
    summary = reconstruct_capture(
        _capture(tmp_path / "capture"),
        tmp_path / "run",
        dry_run=True,
    )

    assert summary["decision"] == "dry_run"
    assert summary["recipe"]["name"] == "desk"
    by_name = {stage["name"]: stage for stage in summary["stages"]}
    assert by_name["train"]["decision"] == "blocked"
    assert by_name["qa"]["decision"] == "blocked"
    saved = load_json_strict(tmp_path / "run/capture_splat_reconstruction_summary.json")
    assert saved["authority"]["quality_claim"] is False


def test_reconstruct_runs_and_resumes_the_evidence_stages(tmp_path: Path, monkeypatch) -> None:
    capture = _capture(tmp_path / "capture")
    backend_root = tmp_path / "vksplat"
    backend_root.mkdir()
    (backend_root / "simple_trainer.py").write_text("# fixture\n", encoding="utf-8")
    rendered_gaussian = tmp_path / "rendered.ply"
    rendered_gaussian.write_bytes(b"pruned")
    render_dir = tmp_path / "renders"
    render_dir.mkdir()
    (render_dir / "000001.png").write_bytes(b"render")
    write_json_strict(render_dir / "capture_splat_render_provenance.json", {
        "schema": "capture_splat.render_provenance.v0.1",
        "gaussian_checksum": _sha256(rendered_gaussian),
    })

    def fake_prepare(capture_dir, out_dir, recipe="auto"):
        (out_dir / "frames/images").mkdir(parents=True)
        (out_dir / "frames/images/000001.jpg").write_bytes(b"image")
        write_json_strict(out_dir / "frames/capture.json", load_json_strict(capture_dir / "capture.json"))
        summary = {
            "schema": "capture_splat.prepare_capture_summary.v0.1",
            "decision": "ready",
            "sfm_request": {
                "images": "frames/images",
                "method": "glomap",
                "features": "sift",
                "matcher": "exhaustive",
                "background_sphere": False,
            },
        }
        write_json_strict(out_dir / "capture_splat_prepare_summary.json", summary)
        return summary

    def fake_sfm(images_dir, out_dir, **kwargs):
        (out_dir / "images").mkdir(parents=True)
        (out_dir / "images/000001.jpg").write_bytes(b"image")
        (out_dir / "sparse/0").mkdir(parents=True)
        for name in ("cameras.txt", "images.txt", "points3D.txt"):
            (out_dir / "sparse/0" / name).write_text("# fixture\n", encoding="utf-8")
        summary = {"schema": "capture_splat.sfm_summary.v0.1", "decision": "promote", "registered_ratio": 0.9}
        write_json_strict(out_dir / "capture_splat_sfm_summary.json", summary)
        return summary

    def fake_seed(capture_dir, package_dir, out_dir):
        package = out_dir / "package"
        package.mkdir(parents=True)
        summary = {
            "schema": "capture_splat.rgbd_seed_summary.v0.1",
            "decision": "promote",
            "package_augmented": True,
            "output_package": str(package),
        }
        write_json_strict(out_dir / "capture_splat_rgbd_seed_summary.json", summary)
        return summary

    def fake_ladder(package_dir, out_dir, root, steps, stop_reset_at=None):
        ply = out_dir / "step_0003000/splat.ply"
        ply.parent.mkdir(parents=True)
        ply.write_bytes(b"ply")
        summary = {
            "schema": "capture_splat.vksplat_ladder_summary.v0.1",
            "decision": "hold",
            "rungs": [{
                "step": 3000,
                "finite_ply": True,
                "splat_ply": str(ply),
                "decision": "hold",
                "reasons": ["finite_output_without_render_source_qa"],
            }],
        }
        write_json_strict(out_dir / "capture_splat_vksplat_ladder_summary.json", summary)
        return summary

    def fake_prune(source, output, min_alpha, max_dropped_fraction):
        output.write_bytes(b"pruned")
        report = {
            "schema": "capture_splat.ply_prune_report.v0.1",
            "decision": "pruned",
            "output": str(output),
        }
        write_json_strict(output.with_suffix(output.suffix + ".prune_report.json"), report)
        return report

    def fake_qa(source_dir, render_dir, out_dir, pairs_json=None):
        summary = {
            "schema": "capture_splat.render_source_qa.v0.1",
            "decision": "promote",
            "frame_count": 1,
        }
        out_dir.mkdir(parents=True)
        write_json_strict(out_dir / "capture_splat_render_source_qa_summary.json", summary)
        return summary

    def fake_export(package, out_dir, **kwargs):
        gaussian = kwargs["gaussian"]
        out_dir.mkdir(parents=True)
        copied_gaussian = out_dir / "splat.ply"
        copied_gaussian.write_bytes(gaussian.read_bytes())
        source = out_dir / "images/000001.jpg"
        source.parent.mkdir()
        source.write_bytes(b"source")
        manifest = {
            "schema": "capture_splat.world_studio_handoff.v0.2",
            "status": "visual_evidence_with_3dgs_proposal",
            "source_frames": [{
                "rgb_path": "images/000001.jpg",
                "size_bytes": source.stat().st_size,
                "checksum": _sha256(source),
            }],
            "assets": {"gaussian_ply": {
                "path": "splat.ply",
                "size_bytes": copied_gaussian.stat().st_size,
                "checksum": _sha256(gaussian),
            }},
        }
        write_json_strict(out_dir / "capture-splat.world-studio.json", manifest)
        return manifest

    monkeypatch.setattr("capture_splat.reconstruct.prepare_capture", fake_prepare)
    monkeypatch.setattr("capture_splat.reconstruct.run_sfm", fake_sfm)
    monkeypatch.setattr("capture_splat.reconstruct.build_rgbd_metric_seed", fake_seed)
    monkeypatch.setattr("capture_splat.reconstruct.run_vksplat_ladder", fake_ladder)
    monkeypatch.setattr("capture_splat.reconstruct.prune_ply_by_alpha", fake_prune)
    monkeypatch.setattr("capture_splat.reconstruct.run_render_source_qa", fake_qa)
    monkeypatch.setattr("capture_splat.reconstruct.export_world_studio_handoff", fake_export)

    run = tmp_path / "run"
    summary = reconstruct_capture(
        capture,
        run,
        backend_root=backend_root,
        steps=[3000],
        qa_render_dir=render_dir,
    )

    assert summary["decision"] == "promote"
    assert [stage["name"] for stage in summary["stages"]] == [
        "prepare", "sfm", "seed", "train", "prune", "qa", "export"
    ]
    assert all(stage.get("resumed") is False for stage in summary["stages"])

    def must_not_run(*args, **kwargs):
        raise AssertionError("completed stage should have resumed")

    for name in (
        "prepare_capture",
        "run_sfm",
        "build_rgbd_metric_seed",
        "run_vksplat_ladder",
        "prune_ply_by_alpha",
        "run_render_source_qa",
        "export_world_studio_handoff",
    ):
        monkeypatch.setattr(f"capture_splat.reconstruct.{name}", must_not_run)

    resumed = reconstruct_capture(
        capture,
        run,
        backend_root=backend_root,
        steps=[3000],
        qa_render_dir=render_dir,
        resume=True,
    )

    assert resumed["decision"] == "promote"
    assert all(stage.get("resumed") is True for stage in resumed["stages"])


def test_promotion_preserves_capture_and_sfm_holds() -> None:
    rung = {"decision": "hold", "reasons": ["finite_output_without_render_source_qa"]}

    blockers = _promotion_blockers(
        {"decision": "hold"},
        {"decision": "hold"},
        rung,
        "pruned",
        "promote",
    )

    assert blockers == ["capture_preparation_not_ready", "sfm_not_promoted"]


def test_selected_splat_prefers_promoted_rung_over_later_hold(tmp_path: Path) -> None:
    promoted = tmp_path / "7000.ply"
    held = tmp_path / "15000.ply"
    promoted.write_bytes(b"promoted")
    held.write_bytes(b"held")

    path, rung = _selected_splat({"rungs": [
        {"step": 7000, "finite_ply": True, "decision": "promote", "splat_ply": str(promoted)},
        {"step": 15000, "finite_ply": True, "decision": "hold", "splat_ply": str(held)},
    ]})

    assert path == promoted
    assert rung is not None and rung["step"] == 7000


def test_resume_allows_configuration_for_unfinished_stages() -> None:
    prior = {
        "run_config": {"capture_manifest": "capture.json", "backend_root": None},
    }
    current = {"capture_manifest": "capture.json", "backend_root": "/external/vksplat"}

    assert _resume_config_mismatches(prior, current, {"prepare": {}}) == []
    assert _resume_config_mismatches(prior, current, {"train": {}}) == ["train:backend_root"]

    prior["run_config"].update({"qa_provenance_json": None, "qa_provenance_checksum": None})
    current.update({"qa_provenance_json": "/renders/provenance.json", "qa_provenance_checksum": "sha256:new"})
    assert _resume_config_mismatches(prior, current, {"qa": {"provenance_verified": False}}) == []


def test_render_digest_and_provenance_detect_changed_evidence(tmp_path: Path) -> None:
    renders = tmp_path / "renders"
    renders.mkdir()
    image = renders / "frame.png"
    image.write_bytes(b"first")
    first = _render_set_digest(renders)
    image.write_bytes(b"second")

    assert _render_set_digest(renders) != first

    gaussian = tmp_path / "splat.ply"
    gaussian.write_bytes(b"ply")
    provenance = tmp_path / "provenance.json"
    provenance.write_text("[]", encoding="utf-8")
    accepted, reason, _ = _qa_provenance(provenance, gaussian)
    assert accepted is False
    assert reason == "render_provenance_invalid"


def test_handoff_validation_hashes_all_declared_assets(tmp_path: Path) -> None:
    gaussian = tmp_path / "source.ply"
    gaussian.write_bytes(b"gaussian")
    handoff_dir = tmp_path / "handoff"
    handoff_dir.mkdir()
    copied = handoff_dir / "splat.ply"
    copied.write_bytes(gaussian.read_bytes())
    frame = handoff_dir / "frame.jpg"
    frame.write_bytes(b"frame")
    handoff = {
        "source_frames": [{"rgb_path": "frame.jpg", "checksum": _sha256(frame)}],
        "assets": {"gaussian_ply": {"path": "splat.ply", "checksum": _sha256(copied)}},
    }

    assert _handoff_assets_valid(handoff, gaussian, handoff_dir) is True
    frame.write_bytes(b"changed")
    assert _handoff_assets_valid(handoff, gaussian, handoff_dir) is False
