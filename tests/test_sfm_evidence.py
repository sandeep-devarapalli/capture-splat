import sqlite3
import struct
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
from PIL import Image

from capture_splat.sfm_evidence import (
    apply_camera_priors,
    camera_evidence_report,
    external_camera_options,
    filter_hloc_features_by_masks,
    photometric_evidence_report,
    write_fixed_evaluation_set,
)


def _images_and_evidence(root: Path) -> tuple[Path, dict[str, dict]]:
    images = root / "images"
    images.mkdir()
    evidence = {}
    for index, cx in ((1, 4.0), (2, 4.25)):
        name = f"{index:06d}.jpg"
        Image.new("RGB", (8, 6), (20 * index, 40, 60)).save(images / name)
        evidence[name] = {
            "rgb": f"images/{name}",
            "intrinsics": {"fl_x": 8.0, "fl_y": 8.0, "cx": cx, "cy": 3.0, "w": 8, "h": 6},
        }
    return images, evidence


def test_camera_report_and_database_priors_keep_per_frame_principal_points(tmp_path: Path) -> None:
    images, evidence = _images_and_evidence(tmp_path)
    database = tmp_path / "database.db"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE cameras(camera_id INTEGER PRIMARY KEY, model INTEGER, width INTEGER, height INTEGER, params BLOB, prior_focal_length INTEGER)")
    connection.execute("CREATE TABLE images(image_id INTEGER PRIMARY KEY, name TEXT, camera_id INTEGER)")
    for index in (1, 2):
        connection.execute("INSERT INTO cameras VALUES (?, 0, 1, 1, ?, 0)", (index, b""))
        connection.execute("INSERT INTO images VALUES (?, ?, ?)", (index, f"{index:06d}.jpg", index))
    connection.commit()
    connection.close()

    report = apply_camera_priors(database, images, evidence)

    assert report["complete"] is True
    assert report["metrics"]["cx"]["span"] == 0.25
    connection = sqlite3.connect(database)
    rows = connection.execute("SELECT model, width, height, params, prior_focal_length FROM cameras ORDER BY camera_id").fetchall()
    connection.close()
    assert [row[:3] for row in rows] == [(1, 8, 6), (1, 8, 6)]
    assert [struct.unpack("<dddd", row[3])[2] for row in rows] == [4.0, 4.25]
    assert all(row[4] == 1 for row in rows)


def test_camera_report_rejects_non_finite_or_missing_intrinsics(tmp_path: Path) -> None:
    images, evidence = _images_and_evidence(tmp_path)
    evidence["000001.jpg"]["intrinsics"]["fl_x"] = float("nan")
    evidence.pop("000002.jpg")

    report = camera_evidence_report(images, evidence)

    assert report["decision"] == "reject"
    assert report["invalid_images"] == ["000001.jpg"]
    assert report["missing_images"] == ["000002.jpg"]


def test_external_camera_options_preserve_real_opencv_distortion(tmp_path: Path) -> None:
    images, evidence = _images_and_evidence(tmp_path)
    for frame in evidence.values():
        frame["intrinsics"].update({
            "camera_model": "OPENCV",
            "k1": 0.01,
            "k2": -0.02,
            "p1": 0.001,
            "p2": -0.001,
        })

    report = camera_evidence_report(images, evidence)
    options = external_camera_options(images, evidence)

    assert report["distortion_coefficients_available"] is True
    assert options is not None
    assert options[0] == "OPENCV"
    assert options[1].endswith("0.01,-0.02,0.001,-0.001")


def test_database_priors_preserve_external_per_frame_opencv_model(tmp_path: Path) -> None:
    images, evidence = _images_and_evidence(tmp_path)
    for frame in evidence.values():
        frame["intrinsics"].update({
            "camera_model": "OPENCV",
            "k1": 0.01,
            "k2": -0.02,
            "p1": 0.001,
            "p2": -0.001,
        })
    database = tmp_path / "database.db"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE cameras(camera_id INTEGER PRIMARY KEY, model INTEGER, width INTEGER, height INTEGER, params BLOB, prior_focal_length INTEGER)")
    connection.execute("CREATE TABLE images(image_id INTEGER PRIMARY KEY, name TEXT, camera_id INTEGER)")
    for index in (1, 2):
        connection.execute("INSERT INTO cameras VALUES (?, 0, 1, 1, ?, 0)", (index, b""))
        connection.execute("INSERT INTO images VALUES (?, ?, ?)", (index, f"{index:06d}.jpg", index))
    connection.commit()
    connection.close()

    apply_camera_priors(database, images, evidence)

    connection = sqlite3.connect(database)
    rows = connection.execute("SELECT model, params FROM cameras ORDER BY camera_id").fetchall()
    connection.close()
    assert [row[0] for row in rows] == [4, 4]
    assert [struct.unpack("<dddddddd", row[1])[-4:] for row in rows] == [
        (0.01, -0.02, 0.001, -0.001),
        (0.01, -0.02, 0.001, -0.001),
    ]


def test_photometric_report_records_coverage_and_rejects_non_finite() -> None:
    report = photometric_evidence_report([
        {"photometric": {"exposure_duration": 0.01, "iso": 100.0, "lens_position": 0.4}},
        {"photometric": {"exposure_duration": float("inf"), "iso": 80.0}},
    ])

    assert report["decision"] == "reject"
    assert report["field_counts"]["lens_position"] == 1
    assert report["non_finite"] == [{"frame": 2, "field": "exposure_duration"}]


def test_fixed_evaluation_set_uses_every_eighth_registered_filename(tmp_path: Path) -> None:
    images_txt = tmp_path / "images.txt"
    lines = ["# header"]
    for index in range(1, 18):
        lines.extend([f"{index} 1 0 0 0 0 0 0 1 {index:06d}.jpg", ""])
    images_txt.write_text("\n".join(lines) + "\n", encoding="utf-8")

    summary = write_fixed_evaluation_set(images_txt, tmp_path / "eval.json")

    assert summary["frames"] == ["000001.jpg", "000009.jpg", "000017.jpg"]


class _FakeDataset:
    def __init__(self, values, attrs=None):
        self.values = np.asarray(values)
        self.attrs = dict(attrs or {})

    @property
    def shape(self):
        return self.values.shape

    def __array__(self, dtype=None, copy=None):
        return np.asarray(self.values, dtype=dtype)


class _FakeGroup(dict):
    name = "/000001.jpg"

    def create_dataset(self, name, data):
        dataset = _FakeDataset(data)
        self[name] = dataset
        return dataset


class _FakeFile:
    def __init__(self, group):
        self.group = group

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def visititems(self, callback):
        callback("000001.jpg", self.group)


def test_hloc_mask_filter_runs_before_matching_contract_and_preserves_attributes(tmp_path: Path, monkeypatch) -> None:
    group = _FakeGroup({
        "keypoints": _FakeDataset([[0, 0], [3, 3]], {"unit": "pixels"}),
        "descriptors": _FakeDataset([[1, 2], [3, 4]]),
        "scores": _FakeDataset([0.9, 0.8]),
        "image_size": _FakeDataset([4, 4]),
    })
    fake_h5py = SimpleNamespace(
        Group=_FakeGroup,
        Dataset=_FakeDataset,
        File=lambda *_args, **_kwargs: _FakeFile(group),
    )
    monkeypatch.setitem(sys.modules, "h5py", fake_h5py)
    masks = tmp_path / "masks"
    masks.mkdir()
    mask = np.full((4, 4), 255, dtype=np.uint8)
    mask[3, 3] = 0
    Image.fromarray(mask).save(masks / "000001.jpg.png")

    report = filter_hloc_features_by_masks(tmp_path / "features.h5", masks)

    assert report["removed_keypoints"] == 1
    assert group["keypoints"].shape == (1, 2)
    assert group["descriptors"].shape == (2, 1)
    assert group["keypoints"].attrs["unit"] == "pixels"


def test_hloc_mask_filter_reports_dimension_mismatch_without_clipping(tmp_path: Path, monkeypatch) -> None:
    group = _FakeGroup({
        "keypoints": _FakeDataset([[0, 0], [3, 3]]),
        "descriptors": _FakeDataset([[1, 2], [3, 4]]),
        "image_size": _FakeDataset([4, 4]),
    })
    monkeypatch.setitem(sys.modules, "h5py", SimpleNamespace(
        Group=_FakeGroup,
        Dataset=_FakeDataset,
        File=lambda *_args, **_kwargs: _FakeFile(group),
    ))
    masks = tmp_path / "masks"
    masks.mkdir()
    Image.fromarray(np.full((2, 2), 255, dtype=np.uint8)).save(masks / "000001.jpg.png")

    report = filter_hloc_features_by_masks(tmp_path / "features.h5", masks)

    assert report["filtered_images"] == 0
    assert report["removed_keypoints"] == 0
    assert report["dimension_mismatches"][0]["feature_size"] == [4, 4]
