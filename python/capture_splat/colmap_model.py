from __future__ import annotations

import hashlib
import math
import struct
from array import array
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Iterable


CONVERSION_SCHEMA = "capture_splat.colmap_binary_text_conversion.v0.1"
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
    for line in path.read_text(encoding="utf-8").splitlines():
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
