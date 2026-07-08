from pathlib import Path

import pytest
from PIL import Image

from capture_splat.json_utils import load_json_strict
from capture_splat.sfm_runner import build_commands, colmap_has_cuda, decide, read_model_stats, run_sfm, select_best_sparse_subdir


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
    assert saved["loop_detection"] is False
    assert saved["colmap_cuda"] is True
    assert saved["cpu_matching_override"] is False
    assert (tmp_path / "out" / "images" / "000001.jpg").exists()
    assert saved["authority"]["quality_claim"] is False


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
    images = tmp_path / "frames"
    write_image(images / "000001.jpg")

    with pytest.raises(RuntimeError, match="hloc"):
        run_sfm(images, tmp_path / "out", matcher="retrieval", dry_run=True)


def test_run_sfm_blocked_without_glomap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("capture_splat.sfm_runner.find_binary", lambda name: "/usr/bin/colmap" if name == "colmap" else None)
    monkeypatch.setattr("capture_splat.sfm_runner.colmap_has_cuda", lambda: True)
    images = tmp_path / "frames"
    write_image(images / "000001.jpg")

    with pytest.raises(RuntimeError, match="glomap_binary_missing"):
        run_sfm(images, tmp_path / "out", method="glomap")
    saved = load_json_strict(tmp_path / "out" / "capture_splat_sfm_summary.json")
    assert saved["decision"] == "blocked"
