from pathlib import Path

import pytest
from PIL import Image

from capture_splat.gsplat_runner import default_photometric_mode
from capture_splat.json_utils import load_json_strict, write_json_strict
from capture_splat.sfm_runner import build_commands, colmap_has_cuda, decide, normalize_method, read_model_stats, run_sfm, select_best_sparse_subdir


@pytest.fixture(autouse=True)
def stable_colmap_capabilities(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "capture_splat.sfm_runner.colmap_capabilities",
        lambda: {"global_mapper": True, "view_graph_calibrator": True, "caspar": False},
    )


def write_image(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (8, 6), (32, 64, 96)).save(path)


def test_build_commands_colmap_sequential_with_loop_vocab(tmp_path: Path) -> None:
    commands = build_commands(tmp_path / "images", tmp_path / "out", "colmap", "sequential", 30, True, tmp_path / "vocab.bin", 8192)

    assert [c[1] for c in commands] == ["feature_extractor", "sequential_matcher", "mapper"]
    matcher = commands[1]
    assert "--SequentialMatching.overlap" in matcher and "30" in matcher
    assert "--SequentialMatching.loop_detection" in matcher
    assert str(tmp_path / "vocab.bin") in matcher
    assert "--ImageReader.single_camera" in commands[0]


def test_build_commands_glomap_appends_registrator(tmp_path: Path) -> None:
    commands = build_commands(tmp_path / "images", tmp_path / "out", "glomap", "exhaustive", 30, False, None, 8192)

    assert commands[1][1] == "exhaustive_matcher"
    assert commands[2][0] == "glomap"
    assert "--Thresholds.min_inlier_num=50" in commands[2]
    assert commands[3][1] == "image_registrator"


def test_build_commands_global_uses_per_frame_priors_masks_and_calibration(tmp_path: Path) -> None:
    masks = tmp_path / "masks"
    commands = build_commands(
        tmp_path / "images", tmp_path / "out", "global", "exhaustive", 30, False, None, 8192,
        camera_policy="per-frame", view_graph_calibration=True, mask_dir=masks,
    )

    assert [command[1] for command in commands] == [
        "feature_extractor", "exhaustive_matcher", "view_graph_calibrator", "global_mapper"
    ]
    assert "--ImageReader.single_camera_per_image" in commands[0]
    assert commands[0][commands[0].index("--ImageReader.mask_path") + 1] == str(masks)
    assert commands[-1][commands[-1].index("--database_path") + 1].endswith("database_global.db")


def test_method_alias_is_incremental() -> None:
    assert normalize_method("colmap") == "incremental"
    assert normalize_method("global") == "global"


def test_build_commands_retrieval_never_uses_exhaustive_matcher(tmp_path: Path) -> None:
    commands = build_commands(tmp_path / "images", tmp_path / "out", "glomap", "retrieval", 30, False, None, 8192)

    assert commands[0][0:3] == ["python-hloc", "extract", "netvlad"]
    assert not any("exhaustive_matcher" in command for command in commands)
    assert commands[-2][0:2] == ["glomap", "mapper"]


def test_select_best_sparse_subdir_promotes_largest(tmp_path: Path) -> None:
    sparse = tmp_path / "sparse"
    (sparse / "0").mkdir(parents=True)
    (sparse / "1").mkdir(parents=True)
    (sparse / "0" / "points3D.bin").write_bytes(b"x" * 10)
    (sparse / "1" / "points3D.bin").write_bytes(b"x" * 100)

    zero = select_best_sparse_subdir(sparse)

    assert zero == sparse / "0"
    assert (sparse / "0" / "points3D.bin").stat().st_size == 100
    assert (sparse / "old_0").exists()


def test_read_model_stats_parses_text_model(tmp_path: Path) -> None:
    sparse = tmp_path / "0"
    sparse.mkdir()
    (sparse / "images.txt").write_text(
        "# header\n1 1 0 0 0 0 0 0 1 a.jpg\n\n2 1 0 0 0 0 0 0 1 b.jpg\n\n",
        encoding="utf-8",
    )
    (sparse / "points3D.txt").write_text(
        "# header\n1 0 0 0 128 128 128 0.5 1 0 2 0\n2 1 1 1 128 128 128 1.5 1 0\n",
        encoding="utf-8",
    )

    stats = read_model_stats(sparse)

    assert stats["registered_images"] == 2
    assert stats["points"] == 2
    assert stats["observations"] == 3
    assert stats["mean_track_length"] == pytest.approx(1.5)
    assert stats["mean_reprojection_error"] == pytest.approx(1.0)


def test_decide_gates() -> None:
    assert decide(59, 100, 0.60, 0.85)[0] == "reject"
    assert decide(70, 100, 0.60, 0.85)[0] == "hold"
    assert decide(90, 100, 0.60, 0.85)[0] == "promote"


def test_colmap_has_cuda_parses_banner(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("capture_splat.sfm_runner.find_binary", lambda name: "/usr/bin/colmap")

    class Completed:
        def __init__(self, stdout: str) -> None:
            self.stdout = stdout
            self.stderr = ""

    monkeypatch.setattr("capture_splat.sfm_runner.subprocess.run", lambda *a, **k: Completed("COLMAP 4.1.0 (Commit Unknown on Unknown with CUDA)"))
    assert colmap_has_cuda() is True
    monkeypatch.setattr("capture_splat.sfm_runner.subprocess.run", lambda *a, **k: Completed("COLMAP 4.0.4 (Commit Unknown on Unknown without CUDA)"))
    assert colmap_has_cuda() is False


def test_run_sfm_dry_run_writes_summary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("capture_splat.sfm_runner.find_binary", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("capture_splat.sfm_runner.colmap_has_cuda", lambda: True)
    images = tmp_path / "frames"
    for index in range(3):
        write_image(images / f"{index:06d}.jpg")

    summary = run_sfm(images, tmp_path / "out", dry_run=True)
    saved = load_json_strict(tmp_path / "out" / "capture_splat_sfm_summary.json")

    assert summary["decision"] == "dry_run"
    assert saved["total_images"] == 3
    assert saved["matcher"] == "exhaustive"
    assert saved["method"] == "global"
    assert saved["view_graph_calibration"]["resolved"] is True
    assert saved["loop_detection"] is False
    assert saved["colmap_cuda"] is True
    assert saved["cpu_matching_override"] is False
    assert (tmp_path / "out" / "images" / "000001.jpg").exists()
    assert saved["authority"]["quality_claim"] is False


def test_run_sfm_auto_uses_per_frame_cameras_only_for_prepared_capture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("capture_splat.sfm_runner.find_binary", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("capture_splat.sfm_runner.colmap_has_cuda", lambda: True)
    capture = tmp_path / "capture"
    images = capture / "images"
    write_image(images / "000001.jpg")
    write_json_strict(capture / "capture.json", {
        "schema": "capture_splat.v0.3",
        "source": "capture_splat.prepare_capture",
        "frames": [{
            "rgb": "images/000001.jpg",
            "transform_matrix": [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]],
            "intrinsics": {"fl_x": 8, "fl_y": 8, "cx": 4, "cy": 3, "w": 8, "h": 6},
        }],
    })

    summary = run_sfm(images, tmp_path / "out", dry_run=True)

    assert summary["camera_policy"]["resolved"] == "per-frame"
    assert summary["view_graph_calibration"]["resolved"] is False
    assert "--ImageReader.single_camera_per_image" in summary["commands"][0]
    assert load_json_strict(tmp_path / "out/capture.json")["source"] == "capture_splat.prepare_capture"
    assert default_photometric_mode(tmp_path / "out") == "bilateral-grid"


def test_run_sfm_generic_external_camera_preserves_distortion_options(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("capture_splat.sfm_runner.find_binary", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("capture_splat.sfm_runner.colmap_has_cuda", lambda: True)
    capture = tmp_path / "capture"
    images = capture / "images"
    write_image(images / "000001.jpg")
    write_json_strict(capture / "capture.json", {
        "schema": "capture_splat.v0.3",
        "source": "transforms_import",
        "frames": [{
            "rgb": "images/000001.jpg",
            "transform_matrix": [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]],
            "intrinsics": {
                "camera_model": "OPENCV", "fl_x": 8, "fl_y": 8, "cx": 4, "cy": 3,
                "w": 8, "h": 6, "k1": 0.01, "k2": -0.02, "p1": 0.001, "p2": -0.001,
            },
        }],
    })

    summary = run_sfm(images, tmp_path / "out", dry_run=True)
    command = summary["commands"][0]

    assert summary["camera_policy"]["resolved"] == "single"
    assert "generic_images_single_camera_fallback" in summary["warnings"]
    assert command[command.index("--ImageReader.camera_model") + 1] == "OPENCV"
    assert command[command.index("--ImageReader.camera_params") + 1].endswith("0.01,-0.02,0.001,-0.001")

    per_frame = run_sfm(images, tmp_path / "out_per_frame", camera_policy="per-frame", dry_run=True)
    assert per_frame["blockers"] == []
    assert "--ImageReader.single_camera_per_image" in per_frame["commands"][0]


def test_run_sfm_blocked_without_cuda(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("capture_splat.sfm_runner.find_binary", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("capture_splat.sfm_runner.colmap_has_cuda", lambda: False)
    images = tmp_path / "frames"
    write_image(images / "000001.jpg")

    with pytest.raises(RuntimeError, match="colmap_cuda_missing"):
        run_sfm(images, tmp_path / "out")
    saved = load_json_strict(tmp_path / "out" / "capture_splat_sfm_summary.json")
    assert saved["decision"] == "blocked"
    assert saved["colmap_cuda"] is False


def test_run_sfm_cpu_matching_override_recorded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("capture_splat.sfm_runner.find_binary", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("capture_splat.sfm_runner.colmap_has_cuda", lambda: False)
    images = tmp_path / "frames"
    write_image(images / "000001.jpg")

    summary = run_sfm(images, tmp_path / "out", allow_cpu_matching=True, dry_run=True)

    assert summary["decision"] == "dry_run"
    assert summary["blockers"] == []
    assert summary["cpu_matching_override"] is True


def test_run_sfm_retrieval_requires_hloc(tmp_path: Path) -> None:
    from capture_splat.json_utils import load_json_strict

    images = tmp_path / "frames"
    write_image(images / "000001.jpg")

    with pytest.raises(RuntimeError, match="hloc_missing"):
        run_sfm(images, tmp_path / "out", matcher="retrieval", features="hloc", dry_run=True)
    summary = load_json_strict(tmp_path / "out/capture_splat_sfm_summary.json")
    assert summary["decision"] == "blocked"
    assert "hloc_missing" in summary["blockers"]
    assert not any("exhaustive_matcher" in command for command in summary["commands"])


def test_run_sfm_retrieval_dry_run_records_hloc_plan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("capture_splat.sfm_runner.find_binary", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("capture_splat.sfm_runner.colmap_has_cuda", lambda: True)
    monkeypatch.setattr("capture_splat.sfm_runner.hloc_status", lambda: {"ready": True})
    images = tmp_path / "frames"
    write_image(images / "000001.jpg")

    summary = run_sfm(
        images,
        tmp_path / "out",
        method="glomap",
        matcher="retrieval",
        features="hloc",
        retrieval_top_k=24,
        dry_run=True,
    )

    assert summary["decision"] == "dry_run"
    assert summary["retrieval_top_k"] == 24
    assert summary["commands"][1][-1] == "24"


def test_run_sfm_rejects_invalid_feature_matcher_pair(tmp_path: Path) -> None:
    images = tmp_path / "frames"
    write_image(images / "000001.jpg")

    with pytest.raises(ValueError, match="requires --features hloc"):
        run_sfm(images, tmp_path / "out", matcher="retrieval")


def test_run_sfm_blocked_without_glomap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("capture_splat.sfm_runner.find_binary", lambda name: "/usr/bin/colmap" if name == "colmap" else None)
    monkeypatch.setattr("capture_splat.sfm_runner.colmap_has_cuda", lambda: True)
    images = tmp_path / "frames"
    write_image(images / "000001.jpg")

    with pytest.raises(RuntimeError, match="glomap_binary_missing"):
        run_sfm(images, tmp_path / "out", method="glomap")
    saved = load_json_strict(tmp_path / "out" / "capture_splat_sfm_summary.json")
    assert saved["decision"] == "blocked"


def test_run_sfm_blocks_requested_caspar_when_not_compiled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("capture_splat.sfm_runner.find_binary", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("capture_splat.sfm_runner.colmap_has_cuda", lambda: True)
    images = tmp_path / "frames"
    write_image(images / "000001.jpg")

    with pytest.raises(RuntimeError, match="colmap_caspar_missing"):
        run_sfm(images, tmp_path / "out", post_ba_backend="caspar", dry_run=True)
