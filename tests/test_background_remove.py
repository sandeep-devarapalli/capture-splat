from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from capture_splat.background_remove import remove_background
from capture_splat.json_utils import load_json_strict


def _fixture(root: Path) -> tuple[Path, Path]:
    images = root / "images"
    masks = root / "masks"
    images.mkdir(parents=True)
    masks.mkdir()
    Image.new("RGB", (4, 2), (100, 50, 20)).save(images / "frame.jpg")
    mask = np.array([[255, 255, 0, 0], [255, 0, 0, 0]], dtype=np.uint8)
    Image.fromarray(mask, mode="L").save(masks / "frame.jpg.png")
    return images, masks


def test_remove_background_uses_prior_mask_and_preserves_source(tmp_path: Path) -> None:
    images, masks = _fixture(tmp_path / "source")

    summary = remove_background(images, tmp_path / "out", mask_dir=masks)

    output = np.asarray(Image.open(tmp_path / "out/images/frame.png"))
    source = np.asarray(Image.open(images / "frame.jpg").convert("RGB"))
    report = load_json_strict(tmp_path / "out/capture_splat_remove_background_summary.json")
    assert summary["mode_resolved"] == "prior"
    assert summary["decision"] == "ready"
    assert summary["records"][0]["foreground_fraction"] == pytest.approx(3 / 8)
    assert output[0, 0].tolist() == [*source[0, 0].tolist(), 255]
    assert output[0, 3].tolist() == [0, 0, 0, 0]
    assert report["premultiplied_alpha"] is True
    assert (images / "frame.jpg").exists()


def test_remove_background_rejects_mask_dimension_mismatch(tmp_path: Path) -> None:
    images, masks = _fixture(tmp_path / "source")
    Image.new("L", (2, 2), 255).save(masks / "frame.jpg.png")

    with pytest.raises(ValueError, match="mask dimension mismatch"):
        remove_background(images, tmp_path / "out", mask_dir=masks, mode="prior")


def test_remove_background_dry_run_holds_when_optional_model_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    images, _ = _fixture(tmp_path / "source")
    monkeypatch.setattr("capture_splat.background_remove._inspyrenet_available", lambda: False)

    summary = remove_background(images, tmp_path / "out", mode="auto", dry_run=True)

    assert summary["mode_resolved"] == "inspyrenet"
    assert summary["decision"] == "hold"
    assert summary["warnings"] == ["transparent_background_missing"]
    assert not (tmp_path / "out/images/frame.png").exists()
