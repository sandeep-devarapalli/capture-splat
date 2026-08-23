import hashlib
import math
import shutil
import struct
import subprocess
from pathlib import Path

import numpy as np
import pytest

from capture_splat.colmap_model import (
    detect_colmap_model_format,
    materialize_colmap_text_model,
    read_colmap_model,
)
from capture_splat.json_utils import load_json_strict
from capture_splat.rgbd_seed import build_rgbd_metric_seed, read_colmap_camera_centers
from tests.test_rgbd_seed import _source_positions, _write_capture


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _assert_binary_rejected(
    sparse: Path,
    output: Path,
    match: str,
) -> None:
    source_hashes = {path.name: _sha256(path) for path in sparse.glob("*.bin")}
    with pytest.raises(ValueError, match=match):
        read_colmap_model(sparse)
    output.mkdir()
    with pytest.raises(ValueError, match=match):
        materialize_colmap_text_model(sparse, output)
    assert not list(output.iterdir())
    assert source_hashes == {path.name: _sha256(path) for path in sparse.glob("*.bin")}


def _write_binary_model(
    sparse: Path,
    centers: np.ndarray | None = None,
    *,
    camera_id: int = 1,
    image_id_start: int = 1,
    point_id: int = 7,
    camera_params: tuple[float, ...] = (4.0, 4.0, 4.0, 3.0),
    observation_xy: tuple[float, float] = (0.25, 0.5),
    observation_point_id: int | None = None,
    point_xyz: tuple[float, float, float] = (2.0, 4.0, 6.0),
    point_error: float = 0.125,
    track: tuple[tuple[int, int], ...] | None = None,
    include_point: bool = True,
    image_names: tuple[str, ...] | None = None,
) -> Path:
    sparse.mkdir(parents=True)
    positions = np.asarray(centers if centers is not None else [[0.0, 0.0, 0.0], [1.0, 0.5, 0.25]])
    (sparse / "cameras.bin").write_bytes(
        struct.pack("<QIiQQ4d", 1, camera_id, 1, 8, 6, *camera_params)
    )
    images = bytearray(struct.pack("<Q", len(positions)))
    linked_point_id = point_id if observation_point_id is None else observation_point_id
    for image_id, center in enumerate(positions, start=image_id_start):
        images.extend(
            struct.pack("<I7dI", image_id, 1.0, 0.0, 0.0, 0.0, *(-center), camera_id)
        )
        name = (
            image_names[image_id - image_id_start]
            if image_names is not None
            else f"{image_id:06d}.jpg"
        )
        images.extend(name.encode("utf-8") + b"\0")
        images.extend(
            struct.pack(
                "<QddQ",
                1,
                *observation_xy,
                (1 << 64) - 1 if linked_point_id == -1 else linked_point_id,
            )
        )
    (sparse / "images.bin").write_bytes(images)
    resolved_track = track if track is not None else tuple(
        (image_id, 0) for image_id in range(image_id_start, image_id_start + len(positions))
    )
    points = bytearray(struct.pack("<Q", int(include_point)))
    if include_point:
        points.extend(
            struct.pack(
                "<QdddBBBdQ",
                point_id,
                *point_xyz,
                10,
                20,
                30,
                point_error,
                len(resolved_track),
            )
        )
    for element in resolved_track:
        points.extend(struct.pack("<II", *element))
    (sparse / "points3D.bin").write_bytes(points)
    return sparse


def test_binary_model_materialization_has_exact_text_parity(tmp_path: Path) -> None:
    sparse = _write_binary_model(
        tmp_path / "sparse/0",
        observation_xy=(-0.0, 0.5),
        point_xyz=(-0.0, 4.0, 6.0),
    )
    source = read_colmap_model(sparse)

    report = materialize_colmap_text_model(sparse, sparse)
    converted = read_colmap_model(sparse, model_format="text")

    assert converted == source
    assert report["source_counts"] == report["output_counts"]
    assert report["source_id_digests"] == report["output_id_digests"]
    assert all(report["parity"].values())
    assert set(report["source_files"]) == {"cameras.bin", "images.bin", "points3D.bin"}
    assert set(report["output_files"]) == {"cameras.txt", "images.txt", "points3D.txt"}
    assert struct.pack("<d", converted.images[1].points2D[0][0]) == struct.pack("<d", -0.0)
    assert struct.pack("<d", converted.points3D[7].xyz[0]) == struct.pack("<d", -0.0)


def test_binary_model_preserves_unsigned_high_bit_ids(tmp_path: Path) -> None:
    sparse = _write_binary_model(
        tmp_path / "sparse/0",
        camera_id=0x80000001,
        image_id_start=0x80000002,
        point_id=0x8000000000000003,
    )

    source = read_colmap_model(sparse)
    report = materialize_colmap_text_model(sparse, sparse)
    converted = read_colmap_model(sparse, model_format="text")

    assert converted == source
    assert set(converted.cameras) == {0x80000001}
    assert set(converted.images) == {0x80000002, 0x80000003}
    assert set(converted.points3D) == {0x8000000000000003}
    assert report["source_id_digests"] == report["output_id_digests"]
    signed_point_id = str(0x8000000000000003 - (1 << 64))
    assert signed_point_id in (sparse / "images.txt").read_text(encoding="utf-8")
    assert signed_point_id in (sparse / "points3D.txt").read_text(encoding="utf-8")


@pytest.mark.skipif(shutil.which("colmap") is None, reason="COLMAP is not installed")
def test_high_bit_point_id_round_trips_through_official_colmap_reader(tmp_path: Path) -> None:
    source_sparse = _write_binary_model(
        tmp_path / "source",
        camera_id=0x80000001,
        image_id_start=0x80000002,
        point_id=0x8000000000000003,
    )
    text_sparse = tmp_path / "text"
    text_sparse.mkdir()
    materialize_colmap_text_model(source_sparse, text_sparse)
    roundtrip_sparse = tmp_path / "roundtrip"
    roundtrip_sparse.mkdir()

    subprocess.run(
        [
            shutil.which("colmap") or "colmap",
            "model_converter",
            "--input_path",
            str(text_sparse),
            "--output_path",
            str(roundtrip_sparse),
            "--output_type",
            "BIN",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    for name in ("rigs.bin", "frames.bin"):
        (roundtrip_sparse / name).unlink(missing_ok=True)

    assert read_colmap_model(roundtrip_sparse, "binary") == read_colmap_model(
        source_sparse, "binary"
    )


@pytest.mark.parametrize("name", ["rigs.bin", "frames.bin"])
def test_binary_model_rejects_unsupported_rig_components(tmp_path: Path, name: str) -> None:
    sparse = _write_binary_model(tmp_path / "sparse/0")
    (sparse / name).write_bytes(struct.pack("<Q", 0))

    with pytest.raises(ValueError, match="lose rig/frame semantics"):
        detect_colmap_model_format(sparse)
    output = tmp_path / "converted"
    output.mkdir()
    with pytest.raises(ValueError, match="lose rig/frame semantics"):
        materialize_colmap_text_model(sparse, output)
    assert not list(output.iterdir())


def test_complete_text_model_with_companion_rig_binary_fails_before_copy(tmp_path: Path) -> None:
    source = _source_positions()
    capture = _write_capture(tmp_path / "capture", source)
    package = tmp_path / "package"
    sparse = _write_binary_model(package / "sparse/0", source)
    materialize_colmap_text_model(sparse, sparse)
    (sparse / "rigs.bin").write_bytes(struct.pack("<Q", 0))
    source_hashes = {path.name: _sha256(path) for path in sparse.glob("*.bin")}

    with pytest.raises(ValueError, match="lose rig/frame semantics"):
        build_rgbd_metric_seed(capture, package, tmp_path / "out")
    assert not (tmp_path / "out").exists()
    assert source_hashes == {path.name: _sha256(path) for path in sparse.glob("*.bin")}


def test_binary_model_maps_unlinked_observation_sentinel(tmp_path: Path) -> None:
    sparse = _write_binary_model(
        tmp_path / "sparse/0",
        observation_point_id=-1,
        include_point=False,
        track=(),
    )

    report = materialize_colmap_text_model(sparse, sparse)
    converted = read_colmap_model(sparse, model_format="text")

    assert converted.points3D == {}
    assert all(image.points2D[0][2] == -1 for image in converted.images.values())
    assert report["source_counts"]["linked_image_observations"] == 0


@pytest.mark.parametrize("stem", ["cameras", "images", "points3D"])
def test_binary_model_rejects_truncated_records(tmp_path: Path, stem: str) -> None:
    sparse = _write_binary_model(tmp_path / "sparse/0")
    path = sparse / f"{stem}.bin"
    path.write_bytes(path.read_bytes()[:-1])

    _assert_binary_rejected(sparse, tmp_path / "converted", "truncated|invalid")


@pytest.mark.parametrize("stem", ["cameras", "images", "points3D"])
def test_binary_model_rejects_trailing_bytes(tmp_path: Path, stem: str) -> None:
    sparse = _write_binary_model(tmp_path / "sparse/0")
    path = sparse / f"{stem}.bin"
    path.write_bytes(path.read_bytes() + b"unexpected")

    _assert_binary_rejected(sparse, tmp_path / "converted", "trailing bytes")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"camera_params": (math.inf, 4.0, 4.0, 3.0)},
        {"centers": np.asarray([[math.inf, 0.0, 0.0], [1.0, 0.5, 0.25]])},
        {"observation_xy": (math.nan, 0.5)},
        {"point_xyz": (math.nan, 4.0, 6.0)},
        {"point_error": math.inf},
    ],
)
def test_binary_model_rejects_non_finite_values(tmp_path: Path, kwargs: dict[str, object]) -> None:
    sparse = _write_binary_model(tmp_path / "sparse/0", **kwargs)

    _assert_binary_rejected(sparse, tmp_path / "converted", "invalid COLMAP")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"observation_point_id": 8},
        {"track": ()},
        {"track": ((1, 1), (2, 0))},
        {"track": ((99, 0),)},
        {"track": ((1, 0), (1, 0))},
    ],
)
def test_binary_model_rejects_invalid_tracks(tmp_path: Path, kwargs: dict[str, object]) -> None:
    sparse = _write_binary_model(tmp_path / "sparse/0", **kwargs)

    _assert_binary_rejected(sparse, tmp_path / "converted", "track")


def test_binary_model_rejects_whitespace_in_image_names(tmp_path: Path) -> None:
    sparse = _write_binary_model(
        tmp_path / "sparse/0",
        image_names=("a bbbb.jpg", "c dddd.jpg"),
    )

    _assert_binary_rejected(sparse, tmp_path / "converted", "unsafe COLMAP image name")


def test_build_rgbd_seed_converts_binary_only_model_in_copied_package(tmp_path: Path) -> None:
    source = _source_positions()
    target = source * 2.0 + np.asarray([1.0, 2.0, 3.0])
    capture = _write_capture(tmp_path / "capture", source)
    package = tmp_path / "package"
    high_point_id = 0x8000000000000003
    sparse = _write_binary_model(package / "sparse/0", target, point_id=high_point_id)
    source_hashes = {path.name: _sha256(path) for path in sparse.glob("*.bin")}

    summary = build_rgbd_metric_seed(capture, package, tmp_path / "out", max_points=100)

    assert summary["decision"] == "promote"
    assert summary["colmap_model_input_format"] == "binary"
    assert not list(sparse.glob("*.txt"))
    assert source_hashes == {path.name: _sha256(path) for path in sparse.glob("*.bin")}
    output_sparse = tmp_path / "out/package/sparse/0"
    assert not list(output_sparse.glob("*.bin"))
    assert {path.name for path in output_sparse.glob("*.txt")} == {
        "cameras.txt", "images.txt", "points3D.txt"
    }
    assert {path.name for path in (tmp_path / "out/package/sparse/0_colmap_refined").glob("*.bin")} == {
        "cameras.bin", "images.bin", "points3D.bin"
    }
    signed_point_id = str(high_point_id - (1 << 64))
    assert signed_point_id in (output_sparse / "points3D.txt").read_text(encoding="utf-8")
    conversion = load_json_strict(tmp_path / "out/package/metadata/colmap_binary_text_conversion.json")
    assert conversion["parity"]["exact_model"] is True
    assert conversion["source_counts"] == conversion["output_counts"]
    assert conversion["source_counts"]["images"] == len(source)
    assert conversion["source_id_digests"] == conversion["output_id_digests"]
    assert conversion["output_files"]["images.txt"]["path"] == "sparse/0_colmap_refined/images.txt"
    assert conversion["source_files"]["images.bin"]["path"] == "sparse/0_colmap_refined/images.bin"
    assert all(
        evidence["checksum"] == f"sha256:{source_hashes[name]}"
        for name, evidence in conversion["source_files"].items()
    )
    for files in (conversion["source_files"], conversion["output_files"]):
        for evidence in files.values():
            path = tmp_path / "out/package" / evidence["path"]
            assert evidence["bytes"] == path.stat().st_size
            assert evidence["checksum"] == f"sha256:{_sha256(path)}"
    centers = read_colmap_camera_centers(output_sparse / "images.txt")
    np.testing.assert_allclose(centers["000001.jpg"], target[0] / 2.0)
    metric = load_json_strict(tmp_path / "out/package/metadata/metric_scale_report.json")
    assert metric["input_checksums"]["colmap_binary_text_conversion_report"].startswith("sha256:")
