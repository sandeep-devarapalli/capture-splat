from pathlib import Path
from tests.test_capture_schema import make_capture
from capture_splat.colmap_export import export_colmap_text


def test_colmap_export_writes_text_model(tmp_path: Path) -> None:
    capture = make_capture(tmp_path / "capture")
    out = tmp_path / "out"
    summary = export_colmap_text(capture, out)
    sparse = out / "sparse" / "0"
    assert summary["image_count"] == 1
    assert (sparse / "cameras.txt").exists()
    assert (sparse / "images.txt").exists()
    assert (sparse / "points3D.txt").exists()
    assert (out / "images" / "000001.jpg").exists()
