import hashlib
import json
import math
import os
import shutil
import struct
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from capture_splat import cli, colmap_model
from capture_splat.colmap_model import (
    IMAGE_ID_NORMALIZATION_REPORT,
    detect_colmap_model_format,
    materialize_colmap_text_model,
    normalize_colmap_image_ids,
    read_colmap_model,
    validate_positive_image_ids,
)
from capture_splat.json_utils import load_json_strict
from capture_splat.rgbd_seed import build_rgbd_metric_seed, read_colmap_camera_centers
from tests.test_rgbd_seed import _source_positions, _write_capture


NORMALIZER_POSIX_ONLY = pytest.mark.skipif(
    not getattr(os, "O_NOFOLLOW", 0),
    reason="the descriptor-pinned normalizer requires POSIX O_NOFOLLOW",
)


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


def _write_zero_based_text_model(sparse: Path, **kwargs: object) -> Path:
    binary = _write_binary_model(
        sparse.parent / f".{sparse.name}-binary",
        image_id_start=0,
        **kwargs,
    )
    sparse.mkdir()
    materialize_colmap_text_model(binary, sparse)
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


@NORMALIZER_POSIX_ONLY
def test_normalize_colmap_image_ids_rewrites_every_track_and_preserves_model(
    tmp_path: Path,
) -> None:
    sparse = _write_zero_based_text_model(tmp_path / "text")
    source = read_colmap_model(sparse)
    source_hashes = {
        path.name: _sha256(path)
        for path in sparse.glob("*.txt")
    }

    report = normalize_colmap_image_ids(sparse, tmp_path / "normalized")
    normalized = read_colmap_model(tmp_path / "normalized", "text")

    with pytest.raises(ValueError, match="positive image IDs"):
        validate_positive_image_ids(source)
    validate_positive_image_ids(normalized)
    assert set(normalized.images) == {1, 2}
    for source_id, output_id in ((0, 1), (1, 2)):
        before = source.images[source_id]
        after = normalized.images[output_id]
        assert (after.qvec, after.tvec, after.camera_id, after.name, after.points2D) == (
            before.qvec,
            before.tvec,
            before.camera_id,
            before.name,
            before.points2D,
        )
    assert normalized.cameras == source.cameras
    assert normalized.points3D[7].track == ((1, 0), (2, 0))
    assert (
        normalized.points3D[7].xyz,
        normalized.points3D[7].rgb,
        normalized.points3D[7].error,
    ) == (
        source.points3D[7].xyz,
        source.points3D[7].rgb,
        source.points3D[7].error,
    )
    assert report["mapping"] == {
        "algorithm": "ascending_source_image_id_to_contiguous_positive_v1",
        "count": 2,
        "changed_count": 2,
        "track_reference_count": 2,
        "changed_track_reference_count": 2,
        "digest": report["mapping"]["digest"],
        "entries": [
            {"source_image_id": 0, "output_image_id": 1},
            {"source_image_id": 1, "output_image_id": 2},
        ],
    }
    assert report["mapping"]["digest"].startswith("sha256:")
    assert report["parity"]["positive_unique_image_ids"] is True
    assert report["authority"]["collision_authority"] is False
    assert report["source_format"] == "text"
    assert str(tmp_path) not in str(report)
    assert load_json_strict(tmp_path / "normalized" / IMAGE_ID_NORMALIZATION_REPORT) == report
    assert source_hashes == {
        path.name: _sha256(path)
        for path in sparse.glob("*.txt")
    }


@NORMALIZER_POSIX_ONLY
def test_normalize_colmap_image_ids_is_deterministic_and_requires_fresh_output(
    tmp_path: Path,
) -> None:
    sparse = _write_zero_based_text_model(tmp_path / "source")
    first = normalize_colmap_image_ids(sparse, tmp_path / "first")
    second = normalize_colmap_image_ids(sparse, tmp_path / "second")

    assert first == second
    for name in (
        "cameras.txt",
        "images.txt",
        "points3D.txt",
        IMAGE_ID_NORMALIZATION_REPORT,
    ):
        assert (tmp_path / "first" / name).read_bytes() == (
            tmp_path / "second" / name
        ).read_bytes()
    with pytest.raises(FileExistsError, match="output exists"):
        normalize_colmap_image_ids(sparse, tmp_path / "first")
    with pytest.raises(ValueError, match="must not be inside"):
        normalize_colmap_image_ids(sparse, sparse / "derived")


@NORMALIZER_POSIX_ONLY
def test_normalize_colmap_image_ids_rejects_binary_and_missing_output_parent(
    tmp_path: Path,
) -> None:
    binary = _write_binary_model(tmp_path / "binary", image_id_start=0)
    source_hashes = {path.name: _sha256(path) for path in binary.glob("*.bin")}

    with pytest.raises(ValueError, match="requires a text model"):
        normalize_colmap_image_ids(binary, tmp_path / "normalized")
    text = _write_zero_based_text_model(tmp_path / "text")
    with pytest.raises(FileNotFoundError, match="output parent missing"):
        normalize_colmap_image_ids(text, tmp_path / "missing" / "normalized")

    assert not (tmp_path / "normalized").exists()
    assert not (tmp_path / "missing").exists()
    assert source_hashes == {
        path.name: _sha256(path) for path in binary.glob("*.bin")
    }


@pytest.mark.parametrize(
    ("constant", "value", "message"),
    [
        ("MAX_COLMAP_IMAGES_BYTES", 1, "source file exceeds"),
        ("MAX_COLMAP_TEXT_LINE_BYTES", 16, "text line exceeds"),
        ("MAX_COLMAP_CAMERAS", 0, "camera count exceeds"),
        ("MAX_COLMAP_IMAGES", 1, "image count exceeds"),
        ("MAX_COLMAP_IMAGE_OBSERVATIONS", 1, "observation count exceeds"),
        ("MAX_COLMAP_TRACK_ELEMENTS", 1, "track element count exceeds"),
    ],
)
@NORMALIZER_POSIX_ONLY
def test_normalize_colmap_image_ids_enforces_source_working_set_bounds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    constant: str,
    value: int,
    message: str,
) -> None:
    source = _write_zero_based_text_model(tmp_path / "source")
    monkeypatch.setattr(colmap_model, constant, value)

    with pytest.raises(ValueError, match=message):
        normalize_colmap_image_ids(source, tmp_path / "normalized")

    assert not (tmp_path / "normalized").exists()


@NORMALIZER_POSIX_ONLY
def test_normalize_colmap_image_ids_bounds_concurrent_source_append(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write_zero_based_text_model(tmp_path / "source")
    images = source / "images.txt"
    target_inode = images.stat().st_ino
    source_bytes = images.stat().st_size
    read = os.read
    appended = False
    read_sizes: list[int] = []

    def append_before_read(descriptor: int, size: int) -> bytes:
        nonlocal appended
        if os.fstat(descriptor).st_ino == target_inode:
            read_sizes.append(size)
            if not appended:
                appended = True
                with images.open("ab") as handle:
                    handle.write(b"unbounded concurrent append")
        return read(descriptor, size)

    monkeypatch.setattr(os, "read", append_before_read)
    with pytest.raises(ValueError, match="changed while snapshotting"):
        normalize_colmap_image_ids(source, tmp_path / "normalized")

    assert appended is True
    assert read_sizes == [source_bytes + 1]
    assert not (tmp_path / "normalized").exists()


@NORMALIZER_POSIX_ONLY
def test_normalize_colmap_image_ids_rejects_case_alias_inside_source(
    tmp_path: Path,
) -> None:
    source = _write_zero_based_text_model(tmp_path / "CaseAliasProbe" / "Sparse")
    alias = source.parent / "sparse"
    if not alias.exists() or not alias.samefile(source):
        pytest.skip("filesystem is case-sensitive")

    with pytest.raises(ValueError, match="must not be inside"):
        normalize_colmap_image_ids(source, alias / "derived")

    assert not (source / "derived").exists()


@pytest.mark.parametrize("component", ["Frames.txt", "RIGS.bin"])
@NORMALIZER_POSIX_ONLY
def test_normalize_colmap_image_ids_rejects_casefold_rig_components(
    tmp_path: Path, component: str,
) -> None:
    source = _write_zero_based_text_model(tmp_path / "source")
    (source / component).write_bytes(b"")

    with pytest.raises(ValueError, match="unsupported COLMAP.*components"):
        normalize_colmap_image_ids(source, tmp_path / "normalized")

    assert not (tmp_path / "normalized").exists()


@NORMALIZER_POSIX_ONLY
def test_normalize_colmap_image_ids_publication_race_preserves_winner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write_zero_based_text_model(tmp_path / "source")
    output = tmp_path / "normalized"
    publish = colmap_model._publish_exclusive_at

    def lose_publication_race(
        parent_descriptor: int, stage_name: str, destination_name: str,
    ) -> None:
        os.mkdir(destination_name, dir_fd=parent_descriptor)
        publish(parent_descriptor, stage_name, destination_name)

    monkeypatch.setattr(colmap_model, "_publish_exclusive_at", lose_publication_race)
    with pytest.raises(FileExistsError):
        normalize_colmap_image_ids(source, output)

    assert output.is_dir()
    assert not list(output.iterdir())
    assert not list(tmp_path.glob(".normalized.*.partial"))


@NORMALIZER_POSIX_ONLY
def test_normalize_colmap_image_ids_stage_swap_does_not_touch_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write_zero_based_text_model(tmp_path / "source")
    source_hashes = {path.name: _sha256(path) for path in source.iterdir()}
    create_stage = colmap_model._create_pinned_stage

    def swap_stage(parent_descriptor: int, output_name: str) -> tuple[str, int]:
        stage_name, stage_descriptor = create_stage(parent_descriptor, output_name)
        moved_name = stage_name + ".moved"
        os.rename(
            stage_name,
            moved_name,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
        )
        os.symlink(source, stage_name, dir_fd=parent_descriptor)
        return stage_name, stage_descriptor

    monkeypatch.setattr(colmap_model, "_create_pinned_stage", swap_stage)
    with pytest.raises(ValueError, match="stage entry changed"):
        normalize_colmap_image_ids(source, tmp_path / "normalized")

    assert not (source / ".source-snapshot").exists()
    assert not (tmp_path / "normalized").exists()
    assert not list(tmp_path.glob(".*.partial.moved"))
    assert source_hashes == {path.name: _sha256(path) for path in source.iterdir()}


@NORMALIZER_POSIX_ONLY
def test_normalize_colmap_image_ids_parent_swap_cannot_publish_attacker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write_zero_based_text_model(tmp_path / "source")
    output_parent = tmp_path / "output-parent"
    output_parent.mkdir()
    moved_parent = tmp_path / "output-parent-moved"
    publish = colmap_model._publish_exclusive_at
    attacker: Path | None = None

    def swap_parent(
        parent_descriptor: int, stage_name: str, destination_name: str,
    ) -> None:
        nonlocal attacker
        output_parent.rename(moved_parent)
        output_parent.mkdir()
        attacker = output_parent / stage_name
        attacker.mkdir()
        (attacker / "attacker.txt").write_bytes(b"attacker")
        publish(parent_descriptor, stage_name, destination_name)

    monkeypatch.setattr(colmap_model, "_publish_exclusive_at", swap_parent)
    with pytest.raises(ValueError, match="output parent changed"):
        normalize_colmap_image_ids(source, output_parent / "normalized")

    assert attacker is not None
    assert (attacker / "attacker.txt").read_bytes() == b"attacker"
    assert not (output_parent / "normalized").exists()
    assert not (moved_parent / "normalized").exists()
    assert not list(moved_parent.glob(".*.partial"))


@pytest.mark.parametrize(
    ("tamper", "message"),
    [("camera", "file changed"), ("extra", "unexpected files")],
)
@NORMALIZER_POSIX_ONLY
def test_normalize_colmap_image_ids_rejects_published_file_tampering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tamper: str,
    message: str,
) -> None:
    source = _write_zero_based_text_model(tmp_path / "source")
    publish = colmap_model._publish_exclusive_at

    def tamper_before_publish(
        parent_descriptor: int, stage_name: str, destination_name: str,
    ) -> None:
        stage_descriptor = os.open(
            stage_name,
            colmap_model._source_flags(directory=True),
            dir_fd=parent_descriptor,
        )
        try:
            name = "cameras.txt" if tamper == "camera" else "unreported.txt"
            flags = os.O_WRONLY | os.O_TRUNC
            if tamper == "extra":
                flags |= os.O_CREAT | os.O_EXCL
            descriptor = os.open(name, flags, 0o600, dir_fd=stage_descriptor)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(b"tampered")
        finally:
            os.close(stage_descriptor)
        publish(parent_descriptor, stage_name, destination_name)

    monkeypatch.setattr(
        colmap_model, "_publish_exclusive_at", tamper_before_publish
    )
    with pytest.raises(ValueError, match=message):
        normalize_colmap_image_ids(source, tmp_path / "normalized")

    assert not (tmp_path / "normalized").exists()


@NORMALIZER_POSIX_ONLY
def test_normalize_colmap_image_ids_rejects_late_source_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write_zero_based_text_model(tmp_path / "source")
    output = tmp_path / "normalized"
    write_report = colmap_model._write_normalization_report

    def write_report_then_mutate(descriptor: int, payload: dict[str, object]) -> None:
        write_report(descriptor, payload)
        with (source / "cameras.txt").open("a", encoding="utf-8") as handle:
            handle.write("late drift")

    monkeypatch.setattr(
        colmap_model, "_write_normalization_report", write_report_then_mutate
    )
    with pytest.raises(ValueError, match="source changed"):
        normalize_colmap_image_ids(source, output)

    assert not output.exists()
    assert not list(tmp_path.glob(".normalized.*.partial"))


@NORMALIZER_POSIX_ONLY
def test_normalize_colmap_image_ids_rejects_source_aba_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write_zero_based_text_model(tmp_path / "source")
    alternate = _write_zero_based_text_model(
        tmp_path / "alternate", point_xyz=(9.0, 8.0, 7.0)
    )
    names = ("cameras.txt", "images.txt", "points3D.txt")
    original = {name: (source / name).read_bytes() for name in names}
    replacement = {name: (alternate / name).read_bytes() for name in names}
    stream = colmap_model._stream_normalized_text_model
    swapped = False

    def stream_during_aba(
        source_descriptor: int,
        image_id_map: dict[int, int],
        output_descriptor: int | None,
    ) -> tuple[dict[str, int], str, int]:
        nonlocal swapped
        if swapped:
            return stream(source_descriptor, image_id_map, output_descriptor)
        swapped = True
        for name in names:
            (source / name).write_bytes(replacement[name])
        try:
            return stream(source_descriptor, image_id_map, output_descriptor)
        finally:
            for name in names:
                (source / name).write_bytes(original[name])

    monkeypatch.setattr(
        colmap_model, "_stream_normalized_text_model", stream_during_aba
    )
    with pytest.raises(ValueError, match="source.*changed"):
        normalize_colmap_image_ids(source, tmp_path / "normalized")

    assert not (tmp_path / "normalized").exists()


@NORMALIZER_POSIX_ONLY
def test_normalize_colmap_image_ids_rejects_invalid_tracks_without_output(
    tmp_path: Path,
) -> None:
    sparse = _write_zero_based_text_model(tmp_path / "source")
    points = (sparse / "points3D.txt").read_text(encoding="utf-8").splitlines()
    fields = points[-1].split()
    fields[9] = "1"
    points[-1] = " ".join(fields)
    (sparse / "points3D.txt").write_text("\n".join(points) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="track"):
        normalize_colmap_image_ids(sparse, tmp_path / "normalized")

    assert not (tmp_path / "normalized").exists()


@NORMALIZER_POSIX_ONLY
def test_normalize_colmap_image_ids_cli_writes_strict_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sparse = _write_zero_based_text_model(tmp_path / "source")
    output = tmp_path / "normalized"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "capture-splat",
            "normalize-colmap-image-ids",
            "--source-sparse",
            str(sparse),
            "--out",
            str(output),
        ],
    )

    cli.main()

    printed = json.loads(capsys.readouterr().out)
    assert printed == load_json_strict(output / IMAGE_ID_NORMALIZATION_REPORT)
    assert printed["schema"] == "capture_splat.colmap_image_id_normalization.v0.1"


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
