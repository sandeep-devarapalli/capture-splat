from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import math
import os
import secrets
import stat
import struct
import unicodedata
from array import array
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Iterable

from .json_utils import ensure_finite, reject_constant


CONVERSION_SCHEMA = "capture_splat.colmap_binary_text_conversion.v0.1"
IMAGE_ID_NORMALIZATION_SCHEMA = "capture_splat.colmap_image_id_normalization.v0.1"
IMAGE_ID_NORMALIZATION_REPORT = "capture_splat_colmap_image_id_normalization.json"
MAX_SPARSE_DIRECTORY_ENTRIES = 1024
MAX_SPARSE_DIRECTORY_NAME_BYTES = 1024 * 1024
MAX_COLMAP_TEXT_LINE_BYTES = 16 * 1024 * 1024
MAX_COLMAP_CAMERAS_BYTES = 64 * 1024 * 1024
MAX_COLMAP_IMAGES_BYTES = 8 * 1024 * 1024 * 1024
MAX_COLMAP_POINTS_BYTES = 8 * 1024 * 1024 * 1024
MAX_COLMAP_SOURCE_BYTES = 12 * 1024 * 1024 * 1024
MAX_COLMAP_CAMERAS = 1_000_000
MAX_COLMAP_IMAGES = 1_000_000
MAX_COLMAP_POINTS = 5_000_000
MAX_COLMAP_OBSERVATIONS_PER_IMAGE = 1_000_000
MAX_COLMAP_IMAGE_OBSERVATIONS = 5_000_000
MAX_COLMAP_TRACKS_PER_POINT = 1_000_000
MAX_COLMAP_TRACK_ELEMENTS = 5_000_000
MODEL_STEMS = ("cameras", "images", "points3D")
UNSUPPORTED_BINARY_STEMS = ("rigs", "frames")
INVALID_UINT32 = (1 << 32) - 1
INVALID_UINT64 = (1 << 64) - 1
MAX_INT64 = (1 << 63) - 1
MIN_INT64 = -(1 << 63)
CAMERA_MODELS = {
    0: ("SIMPLE_PINHOLE", 3),
    1: ("PINHOLE", 4),
    2: ("SIMPLE_RADIAL", 4),
    3: ("RADIAL", 5),
    4: ("OPENCV", 8),
    5: ("OPENCV_FISHEYE", 8),
    6: ("FULL_OPENCV", 12),
    7: ("FOV", 5),
    8: ("SIMPLE_RADIAL_FISHEYE", 4),
    9: ("RADIAL_FISHEYE", 5),
    10: ("THIN_PRISM_FISHEYE", 12),
    11: ("RAD_TAN_THIN_PRISM_FISHEYE", 16),
    12: ("SIMPLE_DIVISION", 4),
    13: ("DIVISION", 5),
    14: ("SIMPLE_FISHEYE", 3),
    15: ("FISHEYE", 4),
    16: ("EUCM", 6),
    17: ("EQUIRECTANGULAR", 2),
}
CAMERA_MODEL_IDS = {name: (model_id, count) for model_id, (name, count) in CAMERA_MODELS.items()}


@dataclass(frozen=True)
class ColmapCamera:
    camera_id: int
    model_id: int
    model: str
    width: int
    height: int
    params: tuple[float, ...]


@dataclass(frozen=True)
class ColmapImage:
    image_id: int
    qvec: tuple[float, float, float, float]
    tvec: tuple[float, float, float]
    camera_id: int
    name: str
    points2D: tuple[tuple[float, float, int], ...]


@dataclass(frozen=True)
class ColmapPoint3D:
    point3D_id: int
    xyz: tuple[float, float, float]
    rgb: tuple[int, int, int]
    error: float
    track: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class ColmapModel:
    cameras: dict[int, ColmapCamera]
    images: dict[int, ColmapImage]
    points3D: dict[int, ColmapPoint3D]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _read_exact(stream: BinaryIO, size: int, label: str) -> bytes:
    value = stream.read(size)
    if len(value) != size:
        raise ValueError(f"truncated COLMAP {label}")
    return value


def _remaining(stream: BinaryIO) -> int:
    position = stream.tell()
    end = stream.seek(0, 2)
    stream.seek(position)
    return end - position


def _bounded_count(stream: BinaryIO, label: str, minimum_record_size: int) -> int:
    count = struct.unpack("<Q", _read_exact(stream, 8, f"{label} count"))[0]
    if count > _remaining(stream) // minimum_record_size:
        raise ValueError(f"invalid COLMAP {label} count")
    return count


def _read_name(stream: BinaryIO) -> str:
    value = bytearray()
    while True:
        byte = _read_exact(stream, 1, "image name")
        if byte == b"\0":
            try:
                return value.decode("utf-8")
            except UnicodeDecodeError as error:
                raise ValueError("COLMAP image name is not UTF-8") from error
        value.extend(byte)
        if len(value) > 65535:
            raise ValueError("COLMAP image name is unbounded")


def _require_end(stream: BinaryIO, name: str) -> None:
    if stream.read(1):
        raise ValueError(f"trailing bytes in COLMAP {name}")


def _reject_unsupported_binary_components(sparse_dir: Path) -> None:
    present = [
        f"{stem}.bin"
        for stem in UNSUPPORTED_BINARY_STEMS
        if (sparse_dir / f"{stem}.bin").exists()
    ]
    if present:
        raise ValueError(
            "unsupported COLMAP binary components would lose rig/frame semantics: "
            + ", ".join(present)
        )


def _binary_cameras(path: Path) -> Iterable[ColmapCamera]:
    with path.open("rb") as stream:
        for _ in range(_bounded_count(stream, "camera", 24)):
            camera_id, model_id, width, height = struct.unpack(
                "<IiQQ", _read_exact(stream, 24, "camera record")
            )
            if model_id not in CAMERA_MODELS:
                raise ValueError(f"unknown COLMAP camera model id: {model_id}")
            model, param_count = CAMERA_MODELS[model_id]
            params = struct.unpack(
                f"<{param_count}d",
                _read_exact(stream, param_count * 8, "camera parameters"),
            )
            yield ColmapCamera(camera_id, model_id, model, width, height, params)
        _require_end(stream, path.name)


def _binary_images(path: Path) -> Iterable[ColmapImage]:
    with path.open("rb") as stream:
        for _ in range(_bounded_count(stream, "image", 73)):
            values = struct.unpack("<I7dI", _read_exact(stream, 64, "image record"))
            name = _read_name(stream)
            point_count = _bounded_count(stream, "image observation", 24)
            points = tuple(
                (
                    x,
                    y,
                    -1 if point_id == INVALID_UINT64 else point_id,
                )
                for x, y, point_id in (
                    struct.unpack("<ddQ", _read_exact(stream, 24, "image observation"))
                    for _ in range(point_count)
                )
            )
            image_id = values[0]
            yield ColmapImage(
                image_id,
                tuple(values[1:5]),
                tuple(values[5:8]),
                values[8],
                name,
                points,
            )
        _require_end(stream, path.name)


def _binary_points(path: Path) -> Iterable[ColmapPoint3D]:
    with path.open("rb") as stream:
        for _ in range(_bounded_count(stream, "point3D", 51)):
            values = struct.unpack("<QdddBBBd", _read_exact(stream, 43, "point3D record"))
            track_count = _bounded_count(stream, "point3D track", 8)
            track = tuple(
                struct.unpack("<II", _read_exact(stream, 8, "point3D track element"))
                for _ in range(track_count)
            )
            point_id = values[0]
            yield ColmapPoint3D(
                point_id,
                tuple(values[1:4]),
                tuple(values[4:7]),
                values[7],
                track,
            )
        _require_end(stream, path.name)


def _collect(records: Iterable[object], id_name: str) -> dict[int, object]:
    output: dict[int, object] = {}
    for record in records:
        record_id = int(getattr(record, id_name))
        if record_id in output:
            raise ValueError(f"duplicate COLMAP {id_name}: {record_id}")
        output[record_id] = record
    return output


def _read_binary_cameras(path: Path) -> dict[int, ColmapCamera]:
    return _collect(_binary_cameras(path), "camera_id")  # type: ignore[return-value]


def _read_binary_images(path: Path) -> dict[int, ColmapImage]:
    return _collect(_binary_images(path), "image_id")  # type: ignore[return-value]


def _read_binary_points(path: Path) -> dict[int, ColmapPoint3D]:
    return _collect(_binary_points(path), "point3D_id")  # type: ignore[return-value]


def _data_lines(path: Path) -> Iterable[str]:
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                yield stripped


def _read_text_cameras(path: Path) -> dict[int, ColmapCamera]:
    cameras: dict[int, ColmapCamera] = {}
    for line in _data_lines(path):
        fields = line.split()
        if len(fields) < 5 or fields[1] not in CAMERA_MODEL_IDS:
            raise ValueError(f"malformed COLMAP camera row: {line}")
        model_id, param_count = CAMERA_MODEL_IDS[fields[1]]
        if len(fields) != 4 + param_count:
            raise ValueError(f"wrong COLMAP camera parameter count: {line}")
        camera_id = int(fields[0])
        if camera_id in cameras:
            raise ValueError(f"duplicate COLMAP camera id: {camera_id}")
        if len(cameras) >= MAX_COLMAP_CAMERAS:
            raise ValueError("COLMAP camera count exceeds its bound")
        cameras[camera_id] = ColmapCamera(
            camera_id,
            model_id,
            fields[1],
            int(fields[2]),
            int(fields[3]),
            tuple(float(value) for value in fields[4:]),
        )
    return cameras


def _read_text_images(path: Path) -> dict[int, ColmapImage]:
    rows = path.read_text(encoding="utf-8").splitlines()
    images: dict[int, ColmapImage] = {}
    index = 0
    while index < len(rows):
        row = rows[index].strip()
        index += 1
        if not row or row.startswith("#"):
            continue
        fields = row.split(maxsplit=9)
        if len(fields) != 10:
            raise ValueError(f"malformed COLMAP image row: {row}")
        if index >= len(rows) or rows[index].lstrip().startswith("#"):
            raise ValueError("COLMAP image row is missing its observations row")
        observations = rows[index].split()
        index += 1
        if len(observations) % 3:
            raise ValueError(f"malformed COLMAP image observations: {rows[index - 1]}")
        points = tuple(
            (
                float(observations[offset]),
                float(observations[offset + 1]),
                _point_id_from_text(observations[offset + 2], allow_unlinked=True),
            )
            for offset in range(0, len(observations), 3)
        )
        image_id = int(fields[0])
        if image_id in images:
            raise ValueError(f"duplicate COLMAP image id: {image_id}")
        images[image_id] = ColmapImage(
            image_id,
            tuple(float(value) for value in fields[1:5]),
            tuple(float(value) for value in fields[5:8]),
            int(fields[8]),
            fields[9],
            points,
        )
    return images


def _read_text_points(path: Path) -> dict[int, ColmapPoint3D]:
    points: dict[int, ColmapPoint3D] = {}
    for line in _data_lines(path):
        fields = line.split()
        if len(fields) < 8 or (len(fields) - 8) % 2:
            raise ValueError(f"malformed COLMAP point3D row: {line}")
        point_id = _point_id_from_text(fields[0])
        if point_id in points:
            raise ValueError(f"duplicate COLMAP point3D id: {point_id}")
        points[point_id] = ColmapPoint3D(
            point_id,
            tuple(float(value) for value in fields[1:4]),
            tuple(int(value) for value in fields[4:7]),
            float(fields[7]),
            tuple(
                (int(fields[offset]), int(fields[offset + 1]))
                for offset in range(8, len(fields), 2)
            ),
        )
    return points


def _validate_name(name: str) -> None:
    path = PurePosixPath(name)
    if (
        not name
        or name != name.strip()
        or path.is_absolute()
        or ".." in path.parts
        or "\\" in name
        or any(character.isspace() for character in name)
        or any(ord(character) < 32 for character in name)
    ):
        raise ValueError(f"unsafe COLMAP image name: {name!r}")


def _validate_camera(camera: ColmapCamera) -> None:
    expected = CAMERA_MODELS.get(camera.model_id)
    if (
        camera.camera_id < 0
        or camera.camera_id >= INVALID_UINT32
        or expected != (camera.model, len(camera.params))
        or camera.width <= 0
        or camera.height <= 0
        or camera.width > 1_000_000
        or camera.height > 1_000_000
        or not all(math.isfinite(value) for value in camera.params)
    ):
        raise ValueError(f"invalid COLMAP camera: {camera.camera_id}")


def _validate_image_header(image: ColmapImage, camera_ids: set[int], names: set[str]) -> None:
    _validate_name(image.name)
    pose = (*image.qvec, *image.tvec)
    qnorm = math.sqrt(sum(value * value for value in image.qvec))
    if (
        image.image_id < 0
        or image.image_id >= INVALID_UINT32
        or image.camera_id not in camera_ids
        or image.name in names
        or not all(math.isfinite(value) for value in pose)
        or not math.isclose(qnorm, 1.0, rel_tol=1e-5, abs_tol=1e-8)
    ):
        raise ValueError(f"invalid COLMAP image: {image.image_id}")


def _validate_observation(image_id: int, x: float, y: float, point_id: int) -> None:
    if (
        not math.isfinite(x)
        or not math.isfinite(y)
        or point_id < -1
        or point_id >= INVALID_UINT64
    ):
        raise ValueError(f"invalid COLMAP image observation: {image_id}")


def _validate_image(image: ColmapImage, camera_ids: set[int], names: set[str]) -> None:
    _validate_image_header(image, camera_ids, names)
    for x, y, point_id in image.points2D:
        _validate_observation(image.image_id, x, y, point_id)


def _validate_point(point: ColmapPoint3D) -> None:
    if (
        point.point3D_id < 0
        or point.point3D_id >= INVALID_UINT64
        or not all(math.isfinite(value) for value in point.xyz)
        or any(value < 0 or value > 255 for value in point.rgb)
        or not math.isfinite(point.error)
        or point.error < 0
        or any(
            image_id < 0
            or image_id >= INVALID_UINT32
            or point2D_index < 0
            or point2D_index >= INVALID_UINT32
            for image_id, point2D_index in point.track
        )
    ):
        raise ValueError(f"invalid COLMAP point3D: {point.point3D_id}")


def validate_colmap_model(model: ColmapModel) -> None:
    camera_ids = set(model.cameras)
    for camera in model.cameras.values():
        _validate_camera(camera)
    names: set[str] = set()
    for image in model.images.values():
        _validate_image(image, camera_ids, names)
        names.add(image.name)
    track_observations: dict[tuple[int, int], int] = {}
    for point in model.points3D.values():
        _validate_point(point)
        for image_id, point2D_index in point.track:
            key = (image_id, point2D_index)
            image = model.images.get(image_id)
            if (
                image is None
                or point2D_index < 0
                or point2D_index >= len(image.points2D)
                or image.points2D[point2D_index][2] != point.point3D_id
                or key in track_observations
            ):
                raise ValueError(f"invalid COLMAP point3D track: {point.point3D_id}")
            track_observations[key] = point.point3D_id
    for image in model.images.values():
        for point2D_index, (_, _, point_id) in enumerate(image.points2D):
            if point_id != -1 and (
                point_id not in model.points3D
                or track_observations.get((image.image_id, point2D_index)) != point_id
            ):
                raise ValueError(f"invalid COLMAP image-to-point3D track: {image.image_id}")


def validate_positive_image_ids(model: ColmapModel) -> None:
    image_ids = {image.image_id for image in model.images.values()}
    invalid_tracks = any(
        image_id <= 0
        for point in model.points3D.values()
        for image_id, _point2D_index in point.track
    )
    if (
        any(image_id <= 0 for image_id in image_ids)
        or len(image_ids) != len(model.images)
        or set(model.images) != image_ids
        or invalid_tracks
    ):
        raise ValueError("COLMAP image IDs and point tracks must use positive image IDs")


def detect_colmap_model_format(sparse_dir: Path) -> str:
    _reject_unsupported_binary_components(sparse_dir)
    text = [sparse_dir / f"{stem}.txt" for stem in MODEL_STEMS]
    binary = [sparse_dir / f"{stem}.bin" for stem in MODEL_STEMS]
    if all(path.is_file() for path in text):
        return "text"
    if any(path.exists() for path in text):
        raise ValueError(f"incomplete COLMAP text model: {sparse_dir}")
    if all(path.is_file() for path in binary):
        return "binary"
    if any(path.exists() for path in binary):
        raise ValueError(f"incomplete COLMAP binary model: {sparse_dir}")
    raise FileNotFoundError(f"COLMAP model missing: {sparse_dir}")


def read_colmap_model(sparse_dir: Path, model_format: str | None = None) -> ColmapModel:
    resolved = model_format or detect_colmap_model_format(sparse_dir)
    if resolved == "binary":
        _reject_unsupported_binary_components(sparse_dir)
        model = ColmapModel(
            _read_binary_cameras(sparse_dir / "cameras.bin"),
            _read_binary_images(sparse_dir / "images.bin"),
            _read_binary_points(sparse_dir / "points3D.bin"),
        )
    elif resolved == "text":
        model = ColmapModel(
            _read_text_cameras(sparse_dir / "cameras.txt"),
            _read_text_images(sparse_dir / "images.txt"),
            _read_text_points(sparse_dir / "points3D.txt"),
        )
    else:
        raise ValueError(f"unsupported COLMAP model format: {resolved}")
    validate_colmap_model(model)
    return model


def _float(value: float) -> str:
    output = repr(value)
    if struct.pack("<d", float(output)) != struct.pack("<d", value):
        raise ValueError("COLMAP float is not text-roundtrippable")
    return output


def _point_id_from_text(token: str, allow_unlinked: bool = False) -> int:
    value = int(token)
    if value < MIN_INT64 or value > MAX_INT64:
        raise ValueError(f"COLMAP text point3D id is outside int64: {token}")
    if allow_unlinked and value == -1:
        return -1
    return value if value >= 0 else value + (1 << 64)


def _point_id_to_text(value: int) -> str:
    if value == -1:
        return "-1"
    if value < 0 or value >= INVALID_UINT64:
        raise ValueError(f"invalid COLMAP point3D id: {value}")
    return str(value if value <= MAX_INT64 else value - (1 << 64))


def _camera_line(camera: ColmapCamera) -> str:
    return " ".join([
        str(camera.camera_id),
        camera.model,
        str(camera.width),
        str(camera.height),
        *(_float(value) for value in camera.params),
    ])


def _image_pose_line(image: ColmapImage) -> str:
    return " ".join([
        str(image.image_id),
        *(_float(value) for value in (*image.qvec, *image.tvec)),
        str(image.camera_id),
        image.name,
    ])


def _point_prefix(point: ColmapPoint3D) -> str:
    return " ".join([
        _point_id_to_text(point.point3D_id),
        *(_float(value) for value in point.xyz),
        *(str(value) for value in point.rgb),
        _float(point.error),
    ])


def _stats(model: ColmapModel) -> dict[str, int]:
    return {
        "cameras": len(model.cameras),
        "images": len(model.images),
        "points3D": len(model.points3D),
        "image_observations": sum(len(image.points2D) for image in model.images.values()),
        "linked_image_observations": sum(
            point_id != -1 for image in model.images.values() for _, _, point_id in image.points2D
        ),
        "track_elements": sum(len(point.track) for point in model.points3D.values()),
    }


def _id_digests_from_sets(
    camera_ids: Iterable[int],
    image_ids: Iterable[int],
    point_ids: Iterable[int],
) -> dict[str, str]:
    def digest(values: Iterable[int], kind: str) -> str:
        output = hashlib.sha256()
        for value in sorted(values):
            output.update(struct.pack(kind, value))
        return f"sha256:{output.hexdigest()}"

    return {
        "camera_ids": digest(camera_ids, "<I"),
        "image_ids": digest(image_ids, "<I"),
        "point3D_ids": digest(point_ids, "<Q"),
    }


def _id_digests(model: ColmapModel) -> dict[str, str]:
    return _id_digests_from_sets(model.cameras, model.images, model.points3D)


def _file_evidence_at(descriptor: int, name: str) -> dict[str, object]:
    file_descriptor = os.open(name, _source_flags(), dir_fd=descriptor)
    try:
        metadata = os.fstat(file_descriptor)
        return {
            "path": name,
            "bytes": metadata.st_size,
            "checksum": _sha256_descriptor(file_descriptor),
        }
    finally:
        os.close(file_descriptor)


def _identity(metadata: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _inode(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _directory_identity(metadata: os.stat_result) -> tuple[int, int, int]:
    return metadata.st_dev, metadata.st_ino, metadata.st_mode


def _source_flags(*, directory: bool = False) -> int:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not nofollow:
        raise RuntimeError("COLMAP normalization requires O_NOFOLLOW support")
    flags = os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0)
    if directory:
        flags |= getattr(os, "O_DIRECTORY", 0)
    return flags


def _portable_key(value: str) -> str:
    return unicodedata.normalize("NFC", value.casefold())


def _bounded_directory_names(
    descriptor: int, label: str = "COLMAP sparse directory"
) -> list[str]:
    names: list[str] = []
    name_bytes = 0
    with os.scandir(descriptor) as entries:
        for entry in entries:
            names.append(entry.name)
            name_bytes += len(entry.name.encode("utf-8"))
            if (
                len(names) > MAX_SPARSE_DIRECTORY_ENTRIES
                or name_bytes > MAX_SPARSE_DIRECTORY_NAME_BYTES
            ):
                raise ValueError(f"{label} exceeds the scan limit")
    return names


def _validate_source_names(names: list[str], source_names: Iterable[str]) -> None:
    grouped: dict[str, list[str]] = {}
    for name in names:
        grouped.setdefault(_portable_key(name), []).append(name)
    unsupported_keys = {
        _portable_key(f"{stem}.{suffix}")
        for stem in UNSUPPORTED_BINARY_STEMS
        for suffix in ("bin", "txt")
    }
    unsupported = sorted(
        name for name in names if _portable_key(name) in unsupported_keys
    )
    if unsupported:
        raise ValueError(
            "unsupported COLMAP components would retain stale image IDs: "
            + ", ".join(unsupported)
        )
    for expected in source_names:
        aliases = grouped.get(_portable_key(expected), [])
        if aliases != [expected]:
            raise ValueError(
                f"COLMAP source component casing or aliases are invalid: {expected}"
            )


def _snapshot_source_model(
    root_fd: int,
    snapshot_descriptor: int,
    source_names: list[str],
) -> tuple[
    dict[str, dict[str, object]],
    tuple[int, int, int, int, int, int],
    dict[str, tuple[int, int, int, int, int, int]],
]:
    source_files: dict[str, dict[str, object]] = {}
    file_identities: dict[str, tuple[int, int, int, int, int, int]] = {}
    _validate_source_names(_bounded_directory_names(root_fd), source_names)
    root_identity = _identity(os.fstat(root_fd))
    byte_limits = {
        "cameras.txt": MAX_COLMAP_CAMERAS_BYTES,
        "images.txt": MAX_COLMAP_IMAGES_BYTES,
        "points3D.txt": MAX_COLMAP_POINTS_BYTES,
    }
    declared_total_bytes = 0
    copied_total_bytes = 0
    for name in source_names:
        source_fd = os.open(name, _source_flags(), dir_fd=root_fd)
        try:
            metadata = os.fstat(source_fd)
            declared_total_bytes += metadata.st_size
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_size > byte_limits[name]
                or declared_total_bytes > MAX_COLMAP_SOURCE_BYTES
            ):
                raise ValueError(f"COLMAP source file exceeds its bound: {name}")
            file_identities[name] = _identity(metadata)
            digest = hashlib.sha256()
            copied = 0
            output_fd = os.open(
                name,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
                0o600,
                dir_fd=snapshot_descriptor,
            )
            with os.fdopen(output_fd, "wb") as output:
                while chunk := os.read(
                    source_fd,
                    min(1024 * 1024, metadata.st_size - copied + 1),
                ):
                    if (
                        copied + len(chunk) > metadata.st_size
                        or copied + len(chunk) > byte_limits[name]
                        or copied_total_bytes + len(chunk) > MAX_COLMAP_SOURCE_BYTES
                    ):
                        raise ValueError(
                            f"COLMAP source changed while snapshotting: {name}"
                        )
                    output.write(chunk)
                    digest.update(chunk)
                    copied += len(chunk)
                    copied_total_bytes += len(chunk)
            if copied != metadata.st_size:
                raise ValueError(f"COLMAP source changed while snapshotting: {name}")
            source_files[name] = {
                "path": name,
                "bytes": copied,
                "checksum": f"sha256:{digest.hexdigest()}",
            }
        finally:
            os.close(source_fd)
    return source_files, root_identity, file_identities


def _validate_source_snapshot(
    source_sparse_dir: Path,
    root_fd: int,
    source_files: dict[str, dict[str, object]],
    root_identity: tuple[int, int, int, int, int, int],
    file_identities: dict[str, tuple[int, int, int, int, int, int]],
) -> None:
    reopened = os.open(source_sparse_dir, _source_flags(directory=True))
    try:
        if (
            _identity(os.fstat(root_fd)) != root_identity
            or _identity(os.fstat(reopened)) != root_identity
        ):
            raise ValueError("COLMAP source directory changed during image-ID normalization")
        _validate_source_names(
            _bounded_directory_names(root_fd), source_files.keys()
        )
        validated_total_bytes = 0
        for name, evidence in source_files.items():
            source_fd = os.open(name, _source_flags(), dir_fd=root_fd)
            try:
                if _identity(os.fstat(source_fd)) != file_identities[name]:
                    raise ValueError(
                        f"COLMAP source changed during image-ID normalization: {name}"
                    )
                digest = hashlib.sha256()
                size = 0
                expected_size = int(evidence["bytes"])
                while chunk := os.read(
                    source_fd,
                    min(1024 * 1024, expected_size - size + 1),
                ):
                    if (
                        size + len(chunk) > expected_size
                        or validated_total_bytes + len(chunk)
                        > MAX_COLMAP_SOURCE_BYTES
                    ):
                        raise ValueError(
                            f"COLMAP source changed during image-ID normalization: {name}"
                        )
                    digest.update(chunk)
                    size += len(chunk)
                    validated_total_bytes += len(chunk)
                if (
                    size != evidence["bytes"]
                    or f"sha256:{digest.hexdigest()}" != evidence["checksum"]
                ):
                    raise ValueError(
                        f"COLMAP source changed during image-ID normalization: {name}"
                    )
            finally:
                os.close(source_fd)
    finally:
        os.close(reopened)


def _mapping_digest(mapping: dict[int, int]) -> str:
    digest = hashlib.sha256()
    for source_id, output_id in sorted(mapping.items()):
        digest.update(struct.pack("<II", source_id, output_id))
    return f"sha256:{digest.hexdigest()}"


def _open_text_output(descriptor: int, name: str):
    file_descriptor = os.open(
        name,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
        0o600,
        dir_fd=descriptor,
    )
    return os.fdopen(file_descriptor, "w", encoding="utf-8")


def _bounded_text_lines(descriptor: int, name: str) -> Iterable[str]:
    file_descriptor = os.open(name, _source_flags(), dir_fd=descriptor)
    with os.fdopen(file_descriptor, "rb") as handle:
        while raw := handle.readline(MAX_COLMAP_TEXT_LINE_BYTES + 1):
            if len(raw) > MAX_COLMAP_TEXT_LINE_BYTES:
                raise ValueError(f"COLMAP text line exceeds its bound: {name}")
            try:
                yield raw.decode("utf-8")
            except UnicodeDecodeError as error:
                raise ValueError(f"COLMAP text file is not UTF-8: {name}") from error


def _descriptor_data_lines(descriptor: int, name: str) -> Iterable[str]:
    for line in _bounded_text_lines(descriptor, name):
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            yield stripped


def _read_text_cameras_descriptor(descriptor: int) -> dict[int, ColmapCamera]:
    cameras: dict[int, ColmapCamera] = {}
    for line in _descriptor_data_lines(descriptor, "cameras.txt"):
        fields = line.split()
        if len(fields) < 5 or fields[1] not in CAMERA_MODEL_IDS:
            raise ValueError(f"malformed COLMAP camera row: {line}")
        model_id, param_count = CAMERA_MODEL_IDS[fields[1]]
        if len(fields) != 4 + param_count:
            raise ValueError(f"wrong COLMAP camera parameter count: {line}")
        camera_id = int(fields[0])
        if camera_id in cameras:
            raise ValueError(f"duplicate COLMAP camera id: {camera_id}")
        if len(cameras) >= MAX_COLMAP_CAMERAS:
            raise ValueError("COLMAP camera count exceeds its bound")
        cameras[camera_id] = ColmapCamera(
            camera_id,
            model_id,
            fields[1],
            int(fields[2]),
            int(fields[3]),
            tuple(float(value) for value in fields[4:]),
        )
    return cameras


def _text_image_rows(descriptor: int) -> Iterable[tuple[str, str]]:
    rows = iter(_bounded_text_lines(descriptor, "images.txt"))
    for line in rows:
        pose = line.strip()
        if not pose or pose.startswith("#"):
            continue
        try:
            observations = next(rows)
        except StopIteration as error:
            raise ValueError("COLMAP image row is missing its observations row") from error
        if observations.lstrip().startswith("#"):
            raise ValueError("COLMAP image row is missing its observations row")
        yield pose, observations.strip()


def _scan_text_image_ids(descriptor: int) -> set[int]:
    image_ids: set[int] = set()
    for pose, _observations in _text_image_rows(descriptor):
        fields = pose.split(maxsplit=1)
        if len(fields) != 2:
            raise ValueError(f"malformed COLMAP image row: {pose}")
        image_id = int(fields[0])
        if image_id < 0 or image_id >= INVALID_UINT32:
            raise ValueError(f"invalid COLMAP image: {image_id}")
        if image_id in image_ids:
            raise ValueError(f"duplicate COLMAP image id: {image_id}")
        if len(image_ids) >= MAX_COLMAP_IMAGES:
            raise ValueError("COLMAP image count exceeds its bound")
        image_ids.add(image_id)
    return image_ids


def _descriptor_is_within(descriptor: int, root_inode: tuple[int, int]) -> bool:
    current = os.dup(descriptor)
    try:
        while True:
            current_inode = _inode(os.fstat(current))
            if current_inode == root_inode:
                return True
            parent = os.open("..", _source_flags(directory=True), dir_fd=current)
            parent_inode = _inode(os.fstat(parent))
            if parent_inode == current_inode:
                os.close(parent)
                return False
            os.close(current)
            current = parent
    finally:
        os.close(current)


def _validate_output_parent(
    parent: Path,
    descriptor: int,
    identity: tuple[int, int, int],
    source_inode: tuple[int, int],
) -> None:
    current = os.stat(parent, follow_symlinks=False)
    if (
        _directory_identity(current) != identity
        or _directory_identity(os.fstat(descriptor)) != identity
    ):
        raise ValueError("COLMAP normalization output parent changed")
    if _descriptor_is_within(descriptor, source_inode):
        raise ValueError("COLMAP normalization output must not be inside the source model")


def _fresh_output_name(descriptor: int, name: str) -> None:
    if any(
        _portable_key(entry) == _portable_key(name)
        for entry in _bounded_directory_names(descriptor, "COLMAP output parent")
    ):
        raise FileExistsError(f"COLMAP normalization output exists: {name}")


def _create_pinned_stage(parent_descriptor: int, output_name: str) -> tuple[str, int]:
    for _ in range(100):
        name = f".{output_name}.{secrets.token_hex(8)}.partial"
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent_descriptor)
        except FileExistsError:
            continue
        before = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        descriptor = os.open(
            name, _source_flags(directory=True), dir_fd=parent_descriptor
        )
        after = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if (
            _directory_identity(before) == _directory_identity(os.fstat(descriptor))
            and _directory_identity(os.fstat(descriptor)) == _directory_identity(after)
        ):
            return name, descriptor
        _remove_pinned_stage(parent_descriptor, descriptor)
        os.close(descriptor)
        raise ValueError("COLMAP normalization stage changed while it was opened")
    raise FileExistsError("unable to reserve a COLMAP normalization stage")


def _entry_names_for_inode(descriptor: int, inode: tuple[int, int]) -> list[str]:
    matches: list[str] = []
    for name in _bounded_directory_names(descriptor, "COLMAP output parent"):
        metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        if _inode(metadata) == inode:
            matches.append(name)
    return matches


def _remove_pinned_stage(parent_descriptor: int, stage_descriptor: int) -> None:
    _clear_directory(stage_descriptor)
    matches = _entry_names_for_inode(parent_descriptor, _inode(os.fstat(stage_descriptor)))
    if len(matches) == 1:
        os.rmdir(matches[0], dir_fd=parent_descriptor)


def _clear_directory(descriptor: int, depth: int = 0) -> None:
    if depth > 4:
        raise ValueError("COLMAP stage nesting exceeds the cleanup limit")
    for name in _bounded_directory_names(descriptor, "COLMAP stage"):
        metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        if stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode):
            child = os.open(name, _source_flags(directory=True), dir_fd=descriptor)
            try:
                _clear_directory(child, depth + 1)
            finally:
                os.close(child)
            os.rmdir(name, dir_fd=descriptor)
        else:
            os.unlink(name, dir_fd=descriptor)


def _publish_exclusive_at(
    parent_descriptor: int, source_name: str, destination_name: str
) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    if hasattr(libc, "renameatx_np"):
        result = libc.renameatx_np(
            ctypes.c_int(parent_descriptor),
            os.fsencode(source_name),
            ctypes.c_int(parent_descriptor),
            os.fsencode(destination_name),
            ctypes.c_uint(0x00000004),
        )
    elif hasattr(libc, "renameat2"):
        result = libc.renameat2(
            ctypes.c_int(parent_descriptor),
            os.fsencode(source_name),
            ctypes.c_int(parent_descriptor),
            os.fsencode(destination_name),
            ctypes.c_uint(1),
        )
    else:
        raise OSError(
            errno.ENOTSUP,
            "exclusive directory rename is unavailable",
            destination_name,
        )
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), destination_name)


def _sha256_descriptor(descriptor: int) -> str:
    digest = hashlib.sha256()
    while chunk := os.read(descriptor, 1024 * 1024):
        digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _write_normalization_report(
    stage_descriptor: int, report: dict[str, object]
) -> None:
    ensure_finite(report)
    text = json.dumps(report, indent=2, sort_keys=True, allow_nan=False)
    json.loads(text, parse_constant=reject_constant)
    with _open_text_output(
        stage_descriptor, IMAGE_ID_NORMALIZATION_REPORT
    ) as handle:
        handle.write(text + "\n")


def _validate_published_files(
    descriptor: int, expected_files: dict[str, dict[str, object]]
) -> None:
    names = _bounded_directory_names(descriptor, "published COLMAP normalization")
    if (
        set(names) != set(expected_files)
        or len({_portable_key(name) for name in names}) != len(names)
    ):
        raise ValueError("published COLMAP normalization has unexpected files")
    for name, evidence in expected_files.items():
        before = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise ValueError(f"published COLMAP normalization file is invalid: {name}")
        file_descriptor = os.open(name, _source_flags(), dir_fd=descriptor)
        try:
            opened = os.fstat(file_descriptor)
            checksum = _sha256_descriptor(file_descriptor)
            after = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if (
                _identity(before) != _identity(opened)
                or _identity(opened) != _identity(after)
                or opened.st_size != evidence["bytes"]
                or checksum != evidence["checksum"]
            ):
                raise ValueError(
                    f"published COLMAP normalization file changed: {name}"
                )
        finally:
            os.close(file_descriptor)


def _publish_pinned_stage(
    parent: Path,
    parent_descriptor: int,
    parent_identity: tuple[int, int, int],
    source_inode: tuple[int, int],
    stage_name: str,
    stage_descriptor: int,
    destination_name: str,
    expected_files: dict[str, dict[str, object]],
) -> None:
    _validate_output_parent(
        parent, parent_descriptor, parent_identity, source_inode
    )
    if _entry_names_for_inode(
        parent_descriptor, _inode(os.fstat(stage_descriptor))
    ) != [stage_name]:
        raise ValueError("COLMAP normalization stage entry changed")
    _fresh_output_name(parent_descriptor, destination_name)
    _publish_exclusive_at(parent_descriptor, stage_name, destination_name)
    try:
        destination_descriptor = os.open(
            destination_name,
            _source_flags(directory=True),
            dir_fd=parent_descriptor,
        )
        try:
            if _inode(os.fstat(destination_descriptor)) != _inode(
                os.fstat(stage_descriptor)
            ):
                raise ValueError("published COLMAP normalization has the wrong identity")
            _validate_published_files(destination_descriptor, expected_files)
        finally:
            os.close(destination_descriptor)
        _validate_output_parent(
            parent, parent_descriptor, parent_identity, source_inode
        )
    except BaseException:
        _remove_pinned_stage(parent_descriptor, stage_descriptor)
        raise


def _stream_normalized_text_model(
    source_descriptor: int,
    image_id_map: dict[int, int],
    output_descriptor: int | None,
) -> tuple[dict[str, int], str, int]:
    if (
        len(set(image_id_map.values())) != len(image_id_map)
        or any(image_id <= 0 or image_id >= INVALID_UINT32 for image_id in image_id_map.values())
    ):
        raise ValueError("normalized COLMAP image IDs must be unique and positive")

    cameras = _read_text_cameras_descriptor(source_descriptor)
    camera_ids = set(cameras)
    image_ids: set[int] = set()
    point_ids: set[int] = set()
    names: set[str] = set()
    observations: dict[int, array[int]] = {}
    seen_tracks: dict[int, bytearray] = {}
    stats = {
        "cameras": 0,
        "images": 0,
        "points3D": 0,
        "image_observations": 0,
        "linked_image_observations": 0,
        "track_elements": 0,
    }
    semantic = hashlib.sha256(b"capture-splat-colmap-normalized-v1\0")
    changed_track_references = 0

    camera_output = (
        _open_text_output(output_descriptor, "cameras.txt")
        if output_descriptor is not None
        else None
    )
    try:
        if camera_output is not None:
            camera_output.write("# CAMERA_ID, MODEL, WIDTH, HEIGHT, PARAMS[]\n")
        for camera in sorted(cameras.values(), key=lambda value: value.camera_id):
            _validate_camera(camera)
            semantic.update(b"C")
            semantic.update(
                struct.pack(
                    "<IIQQ", camera.camera_id, camera.model_id, camera.width, camera.height
                )
            )
            semantic.update(struct.pack(f"<{len(camera.params)}d", *camera.params))
            if camera_output is not None:
                camera_output.write(_camera_line(camera) + "\n")
            stats["cameras"] += 1
    finally:
        if camera_output is not None:
            camera_output.close()

    image_output = (
        _open_text_output(output_descriptor, "images.txt")
        if output_descriptor is not None
        else None
    )
    try:
        if image_output is not None:
            image_output.write("# IMAGE_ID, QW, QX, QY, QZ, TX, TY, TZ, CAMERA_ID, NAME\n")
            image_output.write("# POINTS2D[] as (X, Y, POINT3D_ID)\n")
        for pose, observation_row in _text_image_rows(source_descriptor):
            if stats["images"] >= MAX_COLMAP_IMAGES:
                raise ValueError("COLMAP image count exceeds its bound")
            fields = pose.split(maxsplit=9)
            if len(fields) != 10:
                raise ValueError(f"malformed COLMAP image row: {pose}")
            image = ColmapImage(
                int(fields[0]),
                tuple(float(value) for value in fields[1:5]),
                tuple(float(value) for value in fields[5:8]),
                int(fields[8]),
                fields[9],
                (),
            )
            if image.image_id in image_ids:
                raise ValueError(f"duplicate COLMAP image id: {image.image_id}")
            _validate_image_header(image, camera_ids, names)
            normalized_id = image_id_map.get(image.image_id)
            if normalized_id is None:
                raise ValueError(
                    f"COLMAP image ID missing from normalization map: {image.image_id}"
                )
            image_ids.add(image.image_id)
            names.add(image.name)

            observation_fields = observation_row.split()
            if len(observation_fields) % 3:
                raise ValueError(f"malformed COLMAP image observations: {observation_row}")
            point_count = len(observation_fields) // 3
            if (
                point_count > MAX_COLMAP_OBSERVATIONS_PER_IMAGE
                or stats["image_observations"] + point_count
                > MAX_COLMAP_IMAGE_OBSERVATIONS
            ):
                raise ValueError("COLMAP image observation count exceeds its bound")
            compact_ids = array("Q")
            seen_tracks[image.image_id] = bytearray(point_count)
            observations[image.image_id] = compact_ids

            name = image.name.encode("utf-8")
            semantic.update(b"I")
            semantic.update(
                struct.pack(
                    "<II7dI",
                    normalized_id,
                    image.camera_id,
                    *image.qvec,
                    *image.tvec,
                    len(name),
                )
            )
            semantic.update(name)
            semantic.update(struct.pack("<Q", point_count))

            if image_output is not None:
                normalized = ColmapImage(
                    normalized_id,
                    image.qvec,
                    image.tvec,
                    image.camera_id,
                    image.name,
                    (),
                )
                image_output.write(_image_pose_line(normalized) + "\n")
            output_tokens: list[str] = []
            wrote_tokens = False
            for offset in range(0, len(observation_fields), 3):
                x = float(observation_fields[offset])
                y = float(observation_fields[offset + 1])
                point_id = _point_id_from_text(
                    observation_fields[offset + 2], allow_unlinked=True
                )
                _validate_observation(image.image_id, x, y, point_id)
                binary_point_id = INVALID_UINT64 if point_id == -1 else point_id
                compact_ids.append(binary_point_id)
                semantic.update(struct.pack("<ddQ", x, y, binary_point_id))
                stats["linked_image_observations"] += int(point_id != -1)
                if image_output is not None:
                    output_tokens.extend((_float(x), _float(y), _point_id_to_text(point_id)))
                    if len(output_tokens) >= 12_288:
                        if wrote_tokens:
                            image_output.write(" ")
                        image_output.write(" ".join(output_tokens))
                        output_tokens.clear()
                        wrote_tokens = True
            if image_output is not None:
                if output_tokens:
                    if wrote_tokens:
                        image_output.write(" ")
                    image_output.write(" ".join(output_tokens))
                image_output.write("\n")
            stats["images"] += 1
            stats["image_observations"] += point_count
    finally:
        if image_output is not None:
            image_output.close()

    if image_ids != set(image_id_map):
        raise ValueError("COLMAP normalization map does not match source image IDs")

    point_output = (
        _open_text_output(output_descriptor, "points3D.txt")
        if output_descriptor is not None
        else None
    )
    try:
        if point_output is not None:
            point_output.write(
                "# POINT3D_ID, X, Y, Z, R, G, B, ERROR, "
                "TRACK[] as (IMAGE_ID, POINT2D_IDX)\n"
            )
        for line in _descriptor_data_lines(source_descriptor, "points3D.txt"):
            if stats["points3D"] >= MAX_COLMAP_POINTS:
                raise ValueError("COLMAP point3D count exceeds its bound")
            fields = line.split()
            if len(fields) < 8 or (len(fields) - 8) % 2:
                raise ValueError(f"malformed COLMAP point3D row: {line}")
            point = ColmapPoint3D(
                _point_id_from_text(fields[0]),
                tuple(float(value) for value in fields[1:4]),
                tuple(int(value) for value in fields[4:7]),
                float(fields[7]),
                (),
            )
            _validate_point(point)
            if point.point3D_id in point_ids:
                raise ValueError(f"duplicate COLMAP point3D id: {point.point3D_id}")
            point_ids.add(point.point3D_id)
            track_count = (len(fields) - 8) // 2
            if (
                track_count > MAX_COLMAP_TRACKS_PER_POINT
                or stats["track_elements"] + track_count > MAX_COLMAP_TRACK_ELEMENTS
            ):
                raise ValueError("COLMAP track element count exceeds its bound")
            semantic.update(b"P")
            semantic.update(
                struct.pack(
                    "<QdddBBBdQ",
                    point.point3D_id,
                    *point.xyz,
                    *point.rgb,
                    point.error,
                    track_count,
                )
            )
            if point_output is not None:
                point_output.write(_point_prefix(point))
            for offset in range(8, len(fields), 2):
                image_id = int(fields[offset])
                point2D_index = int(fields[offset + 1])
                image_observations = observations.get(image_id)
                image_tracks = seen_tracks.get(image_id)
                normalized_id = image_id_map.get(image_id)
                if (
                    image_id < 0
                    or image_id >= INVALID_UINT32
                    or point2D_index < 0
                    or point2D_index >= INVALID_UINT32
                    or image_observations is None
                    or image_tracks is None
                    or normalized_id is None
                    or point2D_index >= len(image_observations)
                    or image_observations[point2D_index] != point.point3D_id
                    or image_tracks[point2D_index]
                ):
                    raise ValueError(f"invalid COLMAP point3D track: {point.point3D_id}")
                image_tracks[point2D_index] = 1
                semantic.update(struct.pack("<II", normalized_id, point2D_index))
                changed_track_references += int(normalized_id != image_id)
                if point_output is not None:
                    point_output.write(f" {normalized_id} {point2D_index}")
            if point_output is not None:
                point_output.write("\n")
            stats["points3D"] += 1
            stats["track_elements"] += track_count
    finally:
        if point_output is not None:
            point_output.close()

    for image_id, image_observations in observations.items():
        if any(
            point_id != INVALID_UINT64 and not seen_tracks[image_id][point2D_index]
            for point2D_index, point_id in enumerate(image_observations)
        ):
            raise ValueError(f"invalid COLMAP image-to-point3D track: {image_id}")
    return stats, f"sha256:{semantic.hexdigest()}", changed_track_references


def normalize_colmap_image_ids(
    source_sparse_dir: Path,
    output_sparse_dir: Path,
) -> dict[str, object]:
    source_sparse_dir = source_sparse_dir.resolve()
    if not source_sparse_dir.is_dir():
        raise FileNotFoundError(f"COLMAP source directory missing: {source_sparse_dir}")
    output_name = output_sparse_dir.name
    if not output_name or output_name in (".", ".."):
        raise ValueError("COLMAP normalization output name is invalid")
    try:
        output_parent = output_sparse_dir.parent.resolve(strict=True)
    except FileNotFoundError as error:
        raise FileNotFoundError(
            f"COLMAP normalization output parent missing: {output_sparse_dir.parent}"
        ) from error
    if not output_parent.is_dir():
        raise NotADirectoryError(f"COLMAP normalization output parent: {output_parent}")

    source_format = detect_colmap_model_format(source_sparse_dir)
    if source_format != "text":
        raise ValueError("COLMAP image-ID normalization currently requires a text model")
    source_names = [f"{stem}.txt" for stem in MODEL_STEMS]
    source_descriptor = os.open(source_sparse_dir, _source_flags(directory=True))
    source_inode = _inode(os.fstat(source_descriptor))
    try:
        parent_descriptor = os.open(output_parent, _source_flags(directory=True))
    except BaseException:
        os.close(source_descriptor)
        raise
    parent_identity = _directory_identity(os.fstat(parent_descriptor))
    stage_descriptor = -1
    snapshot_descriptor = -1
    published = False
    try:
        _validate_output_parent(
            output_parent, parent_descriptor, parent_identity, source_inode
        )
        _fresh_output_name(parent_descriptor, output_name)
        stage_name, stage_descriptor = _create_pinned_stage(
            parent_descriptor, output_name
        )
        os.mkdir(".source-snapshot", mode=0o700, dir_fd=stage_descriptor)
        snapshot_descriptor = os.open(
            ".source-snapshot",
            _source_flags(directory=True),
            dir_fd=stage_descriptor,
        )
        source_files, root_identity, file_identities = _snapshot_source_model(
            source_descriptor, snapshot_descriptor, source_names
        )
        source_inode = root_identity[0], root_identity[1]
        _validate_output_parent(
            output_parent, parent_descriptor, parent_identity, source_inode
        )
        source_image_ids = _scan_text_image_ids(snapshot_descriptor)
        image_id_map = {
            source_id: normalized_id
            for normalized_id, source_id in enumerate(sorted(source_image_ids), start=1)
        }
        source_counts, source_semantic_digest, changed_track_references = (
            _stream_normalized_text_model(
                snapshot_descriptor, image_id_map, stage_descriptor
            )
        )
        output_identity_map = {
            normalized_id: normalized_id for normalized_id in image_id_map.values()
        }
        output_counts, output_semantic_digest, _ = _stream_normalized_text_model(
            stage_descriptor, output_identity_map, None
        )
        if source_counts != output_counts or source_semantic_digest != output_semantic_digest:
            raise ValueError("COLMAP image-ID normalization changed model semantics")
        _clear_directory(snapshot_descriptor)
        os.close(snapshot_descriptor)
        snapshot_descriptor = -1
        os.rmdir(".source-snapshot", dir_fd=stage_descriptor)

        output_files = {
            f"{stem}.txt": _file_evidence_at(
                stage_descriptor, f"{stem}.txt"
            )
            for stem in MODEL_STEMS
        }
        report: dict[str, object] = {
            "schema": IMAGE_ID_NORMALIZATION_SCHEMA,
            "source_format": source_format,
            "output_format": "text",
            "mapping": {
                "algorithm": "ascending_source_image_id_to_contiguous_positive_v1",
                "count": len(image_id_map),
                "changed_count": sum(
                    source_id != output_id
                    for source_id, output_id in image_id_map.items()
                ),
                "track_reference_count": source_counts["track_elements"],
                "changed_track_reference_count": changed_track_references,
                "digest": _mapping_digest(image_id_map),
                "entries": [
                    {"source_image_id": source_id, "output_image_id": output_id}
                    for source_id, output_id in sorted(image_id_map.items())
                ],
            },
            "source_files": source_files,
            "output_files": output_files,
            "source_counts": source_counts,
            "output_counts": output_counts,
            "semantic_digest": source_semantic_digest,
            "parity": {
                "counts": True,
                "cameras": True,
                "poses_and_names": True,
                "image_observations": True,
                "points3D": True,
                "tracks": True,
                "positive_unique_image_ids": True,
            },
            "authority": {
                "registration_evidence": False,
                "quality_claim": False,
                "metric_authority": False,
                "collision_authority": False,
                "navigation_authority": False,
                "physics_authority": False,
            },
        }
        _write_normalization_report(stage_descriptor, report)
        _validate_source_snapshot(
            source_sparse_dir,
            source_descriptor,
            source_files,
            root_identity,
            file_identities,
        )
        expected_files = dict(output_files)
        expected_files[IMAGE_ID_NORMALIZATION_REPORT] = _file_evidence_at(
            stage_descriptor, IMAGE_ID_NORMALIZATION_REPORT
        )
        _publish_pinned_stage(
            output_parent,
            parent_descriptor,
            parent_identity,
            source_inode,
            stage_name,
            stage_descriptor,
            output_name,
            expected_files,
        )
        _validate_source_snapshot(
            source_sparse_dir,
            source_descriptor,
            source_files,
            root_identity,
            file_identities,
        )
        published = True
        return report
    except BaseException:
        if snapshot_descriptor >= 0:
            os.close(snapshot_descriptor)
            snapshot_descriptor = -1
        if stage_descriptor >= 0 and not published:
            _remove_pinned_stage(parent_descriptor, stage_descriptor)
        raise
    finally:
        if snapshot_descriptor >= 0:
            os.close(snapshot_descriptor)
        if stage_descriptor >= 0:
            os.close(stage_descriptor)
        os.close(parent_descriptor)
        os.close(source_descriptor)


def materialize_colmap_text_model(
    source_sparse_dir: Path,
    output_sparse_dir: Path,
) -> dict[str, object]:
    _reject_unsupported_binary_components(source_sparse_dir)
    if not output_sparse_dir.is_dir():
        raise FileNotFoundError(f"COLMAP text output directory missing: {output_sparse_dir}")
    source_files = {
        f"{stem}.bin": {
            "path": f"{stem}.bin",
            "bytes": (source_sparse_dir / f"{stem}.bin").stat().st_size,
            "checksum": _sha256(source_sparse_dir / f"{stem}.bin"),
        }
        for stem in MODEL_STEMS
    }
    targets = {stem: output_sparse_dir / f"{stem}.txt" for stem in MODEL_STEMS}
    temporary = {
        stem: output_sparse_dir / f".{stem}.txt.capture_splat_tmp"
        for stem in MODEL_STEMS
    }
    for path in (*targets.values(), *temporary.values()):
        if path.exists():
            raise FileExistsError(f"COLMAP text model target exists: {path}")
    camera_ids: set[int] = set()
    image_ids: set[int] = set()
    point_ids: set[int] = set()
    names: set[str] = set()
    observations: dict[int, array[int]] = {}
    seen_tracks: dict[int, bytearray] = {}
    source_stats = {
        "cameras": 0,
        "images": 0,
        "points3D": 0,
        "image_observations": 0,
        "linked_image_observations": 0,
        "track_elements": 0,
    }
    created: list[Path] = []
    finalized: list[Path] = []
    try:
        created.append(temporary["cameras"])
        with temporary["cameras"].open("x", encoding="utf-8") as handle:
            handle.write("# CAMERA_ID, MODEL, WIDTH, HEIGHT, PARAMS[]\n")
            for camera in _binary_cameras(source_sparse_dir / "cameras.bin"):
                _validate_camera(camera)
                if camera.camera_id in camera_ids:
                    raise ValueError(f"duplicate COLMAP camera id: {camera.camera_id}")
                camera_ids.add(camera.camera_id)
                source_stats["cameras"] += 1
                handle.write(_camera_line(camera) + "\n")
        created.append(temporary["images"])
        with temporary["images"].open("x", encoding="utf-8") as handle:
            handle.write("# IMAGE_ID, QW, QX, QY, QZ, TX, TY, TZ, CAMERA_ID, NAME\n")
            handle.write("# POINTS2D[] as (X, Y, POINT3D_ID)\n")
            with (source_sparse_dir / "images.bin").open("rb") as stream:
                for _ in range(_bounded_count(stream, "image", 73)):
                    values = struct.unpack(
                        "<I7dI", _read_exact(stream, 64, "image record")
                    )
                    name = _read_name(stream)
                    point_count = _bounded_count(stream, "image observation", 24)
                    image = ColmapImage(
                        values[0],
                        tuple(values[1:5]),
                        tuple(values[5:8]),
                        values[8],
                        name,
                        (),
                    )
                    if image.image_id in image_ids:
                        raise ValueError(f"duplicate COLMAP image id: {image.image_id}")
                    _validate_image_header(image, camera_ids, names)
                    image_ids.add(image.image_id)
                    names.add(image.name)
                    compact_ids = array("Q")
                    handle.write(_image_pose_line(image) + "\n")
                    output_tokens: list[str] = []
                    wrote_tokens = False
                    for _ in range(point_count):
                        x, y, binary_point_id = struct.unpack(
                            "<ddQ", _read_exact(stream, 24, "image observation")
                        )
                        point_id = -1 if binary_point_id == INVALID_UINT64 else binary_point_id
                        _validate_observation(image.image_id, x, y, point_id)
                        compact_ids.append(binary_point_id)
                        source_stats["linked_image_observations"] += int(point_id != -1)
                        output_tokens.extend(
                            (_float(x), _float(y), _point_id_to_text(point_id))
                        )
                        if len(output_tokens) >= 12_288:
                            if wrote_tokens:
                                handle.write(" ")
                            handle.write(" ".join(output_tokens))
                            output_tokens.clear()
                            wrote_tokens = True
                    if output_tokens:
                        if wrote_tokens:
                            handle.write(" ")
                        handle.write(" ".join(output_tokens))
                    handle.write("\n")
                    observations[image.image_id] = compact_ids
                    seen_tracks[image.image_id] = bytearray(point_count)
                    source_stats["images"] += 1
                    source_stats["image_observations"] += point_count
                _require_end(stream, "images.bin")
        created.append(temporary["points3D"])
        with temporary["points3D"].open("x", encoding="utf-8") as handle:
            handle.write(
                "# POINT3D_ID, X, Y, Z, R, G, B, ERROR, "
                "TRACK[] as (IMAGE_ID, POINT2D_IDX)\n"
            )
            with (source_sparse_dir / "points3D.bin").open("rb") as stream:
                for _ in range(_bounded_count(stream, "point3D", 51)):
                    values = struct.unpack(
                        "<QdddBBBd", _read_exact(stream, 43, "point3D record")
                    )
                    track_count = _bounded_count(stream, "point3D track", 8)
                    point = ColmapPoint3D(
                        values[0],
                        tuple(values[1:4]),
                        tuple(values[4:7]),
                        values[7],
                        (),
                    )
                    _validate_point(point)
                    if point.point3D_id in point_ids:
                        raise ValueError(f"duplicate COLMAP point3D id: {point.point3D_id}")
                    point_ids.add(point.point3D_id)
                    handle.write(_point_prefix(point))
                    for _ in range(track_count):
                        image_id, point2D_index = struct.unpack(
                            "<II", _read_exact(stream, 8, "point3D track element")
                        )
                        image_observations = observations.get(image_id)
                        image_tracks = seen_tracks.get(image_id)
                        if (
                            image_id >= INVALID_UINT32
                            or point2D_index >= INVALID_UINT32
                            or image_observations is None
                            or image_tracks is None
                            or point2D_index >= len(image_observations)
                            or image_observations[point2D_index] != point.point3D_id
                            or image_tracks[point2D_index]
                        ):
                            raise ValueError(f"invalid COLMAP point3D track: {point.point3D_id}")
                        image_tracks[point2D_index] = 1
                        handle.write(f" {image_id} {point2D_index}")
                    handle.write("\n")
                    source_stats["points3D"] += 1
                    source_stats["track_elements"] += track_count
                _require_end(stream, "points3D.bin")
        for image_id, image_observations in observations.items():
            if any(
                point_id != INVALID_UINT64 and not seen_tracks[image_id][point2D_index]
                for point2D_index, point_id in enumerate(image_observations)
            ):
                raise ValueError(f"invalid COLMAP image-to-point3D track: {image_id}")
        for name, evidence in source_files.items():
            path = source_sparse_dir / name
            if evidence["bytes"] != path.stat().st_size or evidence["checksum"] != _sha256(path):
                raise ValueError(f"COLMAP binary source changed during conversion: {name}")
        for stem in MODEL_STEMS:
            temporary[stem].replace(targets[stem])
            finalized.append(targets[stem])
    except Exception:
        for path in (*created, *finalized):
            path.unlink(missing_ok=True)
        raise
    id_digests = _id_digests_from_sets(camera_ids, image_ids, point_ids)
    return {
        "schema": CONVERSION_SCHEMA,
        "source_format": "binary",
        "output_format": "text",
        "source_files": source_files,
        "output_files": {
            f"{stem}.txt": {
                "path": f"{stem}.txt",
                "bytes": (output_sparse_dir / f"{stem}.txt").stat().st_size,
                "checksum": _sha256(output_sparse_dir / f"{stem}.txt"),
            }
            for stem in MODEL_STEMS
        },
        "source_counts": source_stats,
        "output_counts": dict(source_stats),
        "source_id_digests": id_digests,
        "output_id_digests": dict(id_digests),
        "parity": {
            "counts": True,
            "ids": True,
            "cameras": True,
            "poses": True,
            "image_observations": True,
            "points3D": True,
            "tracks": True,
            "exact_model": True,
        },
    }
