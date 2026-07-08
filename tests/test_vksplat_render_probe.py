from pathlib import Path

from PIL import Image

from capture_splat.json_utils import load_json_strict, write_json_strict
from capture_splat.vksplat_render_probe import parse_frame_list, run_vksplat_render_probe


def write_image(path: Path, color: tuple[int, int, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (8, 8), color).save(path)


def make_package(root: Path) -> Path:
    package = root / "package"
    (package / "images").mkdir(parents=True)
    sparse = package / "sparse" / "0"
    sparse.mkdir(parents=True)
    write_image(package / "images" / "000033.jpg", (10, 20, 30))
    write_image(package / "images" / "000076.jpg", (40, 50, 60))
    (sparse / "cameras.txt").write_text("# cameras\n", encoding="utf-8")
    (sparse / "images.txt").write_text("# images\n", encoding="utf-8")
    return package


def test_parse_frame_list_accepts_ids_and_filenames() -> None:
    assert parse_frame_list("000033,000065.jpg, frame_000086.png") == ["000033", "000065", "frame_000086"]


def test_render_probe_dry_run_requests_train_renders(tmp_path: Path, monkeypatch) -> None:
    package = make_package(tmp_path)
    calls = {}

    def fake_run_vksplat(*args, **kwargs):
        calls.update(kwargs)
        return {"dry_run": True, "command": ["fake"]}

    monkeypatch.setattr("capture_splat.vksplat_render_probe.run_vksplat", fake_run_vksplat)

    summary = run_vksplat_render_probe(
        package,
        tmp_path / "out",
        tmp_path / "vksplat",
        frames="000033",
        dry_run=True,
    )

    assert summary["decision"] == "setup"
    assert calls["save_train_renders"] is True
    assert load_json_strict(tmp_path / "out" / "capture_splat_vksplat_render_probe_summary.json")["requested_frames"] == ["000033"]


def test_render_probe_pairs_exact_train_and_val_frames(tmp_path: Path, monkeypatch) -> None:
    package = make_package(tmp_path)
    out = tmp_path / "out"

    def fake_run_vksplat(package_dir, output_root, vksplat_root, **kwargs):
        work = output_root / "run"
        work.mkdir(parents=True)
        (work / "splat.ply").write_text("ply\n", encoding="ascii")
        write_json_strict(
            work / "train.json",
            {
                "train_images": [{"image_path": str(package / "images" / "000033.jpg")}],
                "val_images": [{"image_path": str(package / "images" / "000076.jpg")}],
            },
        )
        write_image(work / "train_00000.png", (10, 20, 30))
        write_image(work / "val_00000.png", (40, 50, 60))
        return {"splat_ply": str(work / "splat.ply"), "returncode": 0}

    monkeypatch.setattr("capture_splat.vksplat_render_probe.run_vksplat", fake_run_vksplat)

    summary = run_vksplat_render_probe(
        package,
        out,
        tmp_path / "vksplat",
        frames="000033,000076,000999",
    )

    assert summary["decision"] == "promote"
    assert "frame_missing:000999" in summary["warnings"]
    pairs = load_json_strict(out / "render_source_pairs.json")["pairs"]
    assert pairs == [
        {"source": "000033.jpg", "render": "train_00000.png"},
        {"source": "000076.jpg", "render": "val_00000.png"},
    ]
    coverage = {frame["frame_id"]: frame for frame in summary["frame_coverage"] if frame.get("available", True)}
    assert coverage["000033"]["split"] == "train"
    assert coverage["000076"]["split"] == "val"
    assert (out / "render_qa" / "capture_splat_render_source_qa_summary.json").exists()
