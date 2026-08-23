from __future__ import annotations

import hashlib
import json
import math
import os
import stat
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from .hybrid_surface import _false_authority
from .json_utils import ensure_finite, reject_constant

REPORT_SCHEMA = "capture_splat.portal_route_derivation.v0.1"
REPORT_NAME = "capture_splat_portal_route_derivation_report.json"
DEFAULT_THROUGH_BAND_METERS = 0.15

_REGIONS = ("side_a", "through_opening", "side_b")
_MAX_PORTALS = 256
_MAX_TRAJECTORY_SAMPLES = 1_000_000
_MAX_DISTANCE_EVALUATIONS = 2_000_000
_MAX_PREPARED_FRAMES = 100_000
_MAX_CAPTURE_JSON_BYTES = 64 * 1024 * 1024
_MAX_AUXILIARY_JSON_BYTES = 16 * 1024 * 1024
_MAX_TRAJECTORY_BYTES = 512 * 1024 * 1024
_MAX_TRAJECTORY_LINE_BYTES = 1024 * 1024
_MAX_COLMAP_IMAGES_BYTES = 64 * 1024 * 1024
_MAX_COLMAP_IMAGE_RECORDS = 1_000_000
_MAX_DIRECTORY_ENTRIES = 200_000
_MAX_DIRECTORY_NAME_BYTES = 64 * 1024 * 1024
_MAX_OPEN_DIRECTORIES_PER_ROOT = 128
_MAX_DIRECTORY_SCANS_PER_ROOT = 512
_MAX_SCANNED_DIRECTORY_ENTRIES_PER_ROOT = 1_000_000
_MAX_SCANNED_DIRECTORY_NAME_BYTES_PER_ROOT = 256 * 1024 * 1024
_MAX_PATH_COMPONENTS = 128
_MAX_PARITY_IMAGE_BYTES = 64 * 1024 * 1024
_MAX_PARITY_COMBINED_BYTES = 16 * 1024 * 1024 * 1024
_MAX_RETAINED_CROSSING_EVENTS = 256
_MAX_REPORT_BYTES = 16 * 1024 * 1024
_MATRIX_TOLERANCE = 1e-4
_COLMAP_QUATERNION_TOLERANCE = 1e-3
_MAX_CROSSING_DELTA_SECONDS = 0.5
_MAX_CROSSING_DISTANCE_METERS = 0.5
_MAX_CROSSING_SPEED_METERS_PER_SECOND = 3.0
_READ_CHUNK_BYTES = 1024 * 1024
_OPEN_DIR_FD_SUPPORTED = os.open in os.supports_dir_fd
_STAT_DIR_FD_SUPPORTED = os.stat in os.supports_dir_fd
_SCANDIR_FD_SUPPORTED = os.scandir in os.supports_fd


def _portable_key(value: str) -> str:
    return unicodedata.normalize("NFC", value.casefold())


def _inode(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _identity(metadata: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _directory_path_identity(metadata: os.stat_result) -> tuple[int, int, int]:
    return metadata.st_dev, metadata.st_ino, metadata.st_mode


def _directory_flags() -> int:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    if not nofollow or not directory or not _OPEN_DIR_FD_SUPPORTED or not _STAT_DIR_FD_SUPPORTED:
        raise RuntimeError("portal derivation requires descriptor-relative O_NOFOLLOW support")
    return os.O_RDONLY | nofollow | directory | getattr(os, "O_CLOEXEC", 0)


def _file_flags(*, write: bool = False, read_write: bool = False) -> int:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not nofollow:
        raise RuntimeError("portal derivation requires O_NOFOLLOW support")
    if write and read_write:
        raise ValueError("file flags cannot request both write-only and read-write access")
    access = os.O_RDWR if read_write else (os.O_WRONLY if write else os.O_RDONLY)
    flags = access | nofollow | getattr(os, "O_CLOEXEC", 0)
    return flags | getattr(os, "O_BINARY", 0)


def _directory_names(
    descriptor: int,
    label: str,
    *,
    scan_budget: _DirectoryScanBudget | None = None,
) -> list[str]:
    if not _SCANDIR_FD_SUPPORTED:
        raise RuntimeError(
            "portal derivation requires descriptor-relative directory scanning support"
        )
    if scan_budget is not None:
        if scan_budget.scans + 1 > _MAX_DIRECTORY_SCANS_PER_ROOT:
            raise ValueError(f"{label} exceeds the aggregate directory scan limit")
        scan_budget.scans += 1
    names: list[str] = []
    name_bytes = 0
    try:
        with os.scandir(descriptor) as entries:
            for entry in entries:
                name = entry.name
                if not isinstance(name, str):
                    raise ValueError(f"{label} directory names are invalid")
                try:
                    encoded_bytes = len(name.encode("utf-8"))
                except UnicodeEncodeError as error:
                    raise ValueError(
                        f"{label} directory name is not strict UTF-8"
                    ) from error
                if (
                    len(names) >= _MAX_DIRECTORY_ENTRIES
                    or name_bytes + encoded_bytes > _MAX_DIRECTORY_NAME_BYTES
                ):
                    raise ValueError(
                        f"{label} directory exceeds the bounded enumeration limit"
                    )
                if scan_budget is not None:
                    if (
                        scan_budget.entries + 1
                        > _MAX_SCANNED_DIRECTORY_ENTRIES_PER_ROOT
                    ):
                        raise ValueError(
                            f"{label} exceeds the aggregate directory entry limit"
                        )
                    if (
                        scan_budget.name_bytes + encoded_bytes
                        > _MAX_SCANNED_DIRECTORY_NAME_BYTES_PER_ROOT
                    ):
                        raise ValueError(
                            f"{label} exceeds the aggregate directory name byte limit"
                        )
                    scan_budget.entries += 1
                    scan_budget.name_bytes += encoded_bytes
                names.append(name)
                name_bytes += encoded_bytes
    except OSError as error:
        raise ValueError(f"{label} directory cannot be enumerated safely") from error
    return names


@dataclass
class _DirectoryScanBudget:
    scans: int = 0
    entries: int = 0
    name_bytes: int = 0

    def names(self, descriptor: int, label: str) -> list[str]:
        return _directory_names(descriptor, label, scan_budget=self)


def _name_index(names: list[str]) -> dict[str, tuple[str, ...]]:
    grouped: dict[str, list[str]] = {}
    for name in names:
        grouped.setdefault(_portable_key(name), []).append(name)
    return {key: tuple(values) for key, values in grouped.items()}


def _require_exact_name(
    names: dict[str, tuple[str, ...]], name: str, label: str
) -> None:
    aliases = names.get(_portable_key(name), ())
    if name not in aliases:
        if aliases:
            raise ValueError(f"{label} physical path component casing does not match: {name}")
        raise FileNotFoundError(f"{label} is missing: {name}")
    if len(aliases) != 1:
        raise ValueError(f"{label} has a casefold path alias: {name}")


def _require_exact_component(
    descriptor: int,
    name: str,
    label: str,
    *,
    scan_budget: _DirectoryScanBudget | None = None,
) -> None:
    names = (
        scan_budget.names(descriptor, label)
        if scan_budget is not None
        else _directory_names(descriptor, label)
    )
    _require_exact_name(_name_index(names), name, label)


def _absolute_parts(path: Path) -> tuple[Path, tuple[str, ...]]:
    absolute = Path(os.path.abspath(os.fspath(path)))
    if not absolute.is_absolute():
        raise ValueError("portal derivation paths must resolve to absolute paths")
    parts = tuple(part for part in absolute.parts if part != absolute.anchor)
    if len(parts) > _MAX_PATH_COMPONENTS:
        raise ValueError("portal derivation path exceeds the component limit")
    return absolute, parts


def _open_absolute_directory(path: Path, label: str) -> tuple[int, Path, tuple[str, ...]]:
    absolute, parts = _absolute_parts(path)
    descriptor = os.open(absolute.anchor, _directory_flags())
    scan_budget = _DirectoryScanBudget()
    try:
        for index, name in enumerate(parts):
            component_label = f"{label} component {'/'.join(parts[: index + 1])}"
            _require_exact_component(
                descriptor,
                name,
                component_label,
                scan_budget=scan_budget,
            )
            before = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
                raise ValueError(f"{component_label} must be a regular non-symlink directory")
            child = os.open(name, _directory_flags(), dir_fd=descriptor)
            opened = os.fstat(child)
            after = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if (
                _directory_path_identity(before) != _directory_path_identity(opened)
                or _directory_path_identity(opened) != _directory_path_identity(after)
            ):
                os.close(child)
                raise ValueError(f"{component_label} changed while it was opened")
            os.close(descriptor)
            descriptor = child
        return descriptor, absolute, parts
    except Exception:
        os.close(descriptor)
        raise


@dataclass(frozen=True)
class _FilePin:
    relative: str
    parent_parts: tuple[str, ...]
    name: str
    identity: tuple[int, int, int, int, int, int]


class _ConfinedRoot:
    def __init__(self, path: Path, label: str) -> None:
        descriptor, absolute, components = _open_absolute_directory(path, label)
        self.path = absolute
        self.components = components
        self.label = label
        self._directories: dict[tuple[str, ...], tuple[int, tuple[int, int]]] = {
            (): (descriptor, _inode(os.fstat(descriptor)))
        }
        self._directory_inodes: dict[tuple[int, int], tuple[str, ...]] = {
            _inode(os.fstat(descriptor)): ()
        }
        self._files: dict[str, _FilePin] = {}
        self._file_inodes: dict[tuple[int, int], str] = {}
        self._portable_paths: dict[str, str] = {}
        self._directory_name_snapshots: dict[
            tuple[str, ...], dict[str, tuple[str, ...]]
        ] = {}
        self._scan_budget = _DirectoryScanBudget()

    def close(self) -> None:
        for descriptor, _ in sorted(
            self._directories.values(), key=lambda item: item[0], reverse=True
        ):
            try:
                os.close(descriptor)
            except OSError:
                pass
        self._directories.clear()

    def __enter__(self) -> _ConfinedRoot:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _directory(self, parts: tuple[str, ...], label: str) -> int:
        if len(parts) > _MAX_PATH_COMPONENTS:
            raise ValueError(f"{label} exceeds the path component limit")
        existing = self._directories.get(parts)
        if existing is not None:
            return existing[0]
        parent_parts = parts[:-1]
        parent = self._directory(parent_parts, label)
        if len(self._directories) >= _MAX_OPEN_DIRECTORIES_PER_ROOT:
            raise ValueError(f"{label} exceeds the open directory limit")
        name = parts[-1]
        self._require_cached_name(parent_parts, name, label)
        before = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
            raise ValueError(f"{label} parent component must be a regular non-symlink directory")
        descriptor = os.open(name, _directory_flags(), dir_fd=parent)
        try:
            opened = os.fstat(descriptor)
            after = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if (
                _directory_path_identity(before) != _directory_path_identity(opened)
                or _directory_path_identity(opened) != _directory_path_identity(after)
            ):
                raise ValueError(f"{label} parent component changed while it was opened")
            inode = _inode(opened)
            alias = self._directory_inodes.get(inode)
            if alias is not None and alias != parts:
                raise ValueError(f"{label} parent component is an inode alias")
        except Exception:
            os.close(descriptor)
            raise
        self._directories[parts] = (descriptor, inode)
        self._directory_inodes[inode] = parts
        return descriptor

    def _require_cached_name(
        self, parent_parts: tuple[str, ...], name: str, label: str
    ) -> None:
        names = self._directory_name_snapshots.get(parent_parts)
        if names is None:
            names = _name_index(
                self._scan_budget.names(self._directories[parent_parts][0], label)
            )
            self._directory_name_snapshots[parent_parts] = names
        _require_exact_name(names, name, label)

    def _open_file(self, relative: Any, label: str, maximum_bytes: int) -> tuple[int, _FilePin]:
        canonical = _canonical_relative_path(relative, label)
        portable = _portable_key(canonical)
        alias = self._portable_paths.get(portable)
        if alias is not None and alias != canonical:
            raise ValueError(f"{label} has a casefold path alias")
        parts = PurePosixPath(canonical).parts
        parent_parts = tuple(parts[:-1])
        name = parts[-1]
        parent = self._directory(parent_parts, label)
        self._require_cached_name(parent_parts, name, label)
        before = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (
            stat.S_ISLNK(before.st_mode)
            or not stat.S_ISREG(before.st_mode)
            or before.st_size > maximum_bytes
        ):
            raise ValueError(f"{label} must be a bounded regular non-symlink file")
        descriptor = os.open(name, _file_flags(), dir_fd=parent)
        try:
            opened = os.fstat(descriptor)
            after = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if _identity(before) != _identity(opened) or _identity(opened) != _identity(after):
                raise ValueError(f"{label} changed while it was opened")
            inode = _inode(opened)
            inode_alias = self._file_inodes.get(inode)
            if inode_alias is not None and inode_alias != canonical:
                raise ValueError(f"{label} is an inode alias of {inode_alias}")
            pin = _FilePin(canonical, parent_parts, name, _identity(opened))
            previous = self._files.get(canonical)
            if previous is not None and previous.identity != pin.identity:
                raise ValueError(f"{label} changed between bounded reads")
            self._portable_paths[portable] = canonical
            self._file_inodes[inode] = canonical
            self._files[canonical] = pin
            return descriptor, pin
        except Exception:
            os.close(descriptor)
            raise

    def _validate_file(self, pin: _FilePin) -> None:
        parent = self._directory(pin.parent_parts, pin.relative)
        current = os.stat(pin.name, dir_fd=parent, follow_symlinks=False)
        if stat.S_ISLNK(current.st_mode) or not stat.S_ISREG(current.st_mode) or _identity(current) != pin.identity:
            raise ValueError(f"{pin.relative} changed while it was read or consumed")

    def snapshot(
        self,
        relative: Any,
        label: str,
        *,
        maximum_bytes: int,
        collect: bool,
        reference_path: str | None = None,
    ) -> tuple[bytes | None, dict[str, Any]]:
        descriptor, pin = self._open_file(relative, label, maximum_bytes)
        digest = hashlib.sha256()
        chunks: list[bytes] | None = [] if collect else None
        bytes_read = 0
        try:
            while chunk := os.read(descriptor, _READ_CHUNK_BYTES):
                bytes_read += len(chunk)
                if bytes_read > maximum_bytes:
                    raise ValueError(f"{label} exceeds the bounded byte limit")
                digest.update(chunk)
                if chunks is not None:
                    chunks.append(chunk)
            if _identity(os.fstat(descriptor)) != pin.identity or bytes_read != pin.identity[3]:
                raise ValueError(f"{label} changed while it was read")
        finally:
            os.close(descriptor)
        self._validate_file(pin)
        return (b"".join(chunks) if chunks is not None else None), {
            "path": reference_path if reference_path is not None else pin.relative,
            "size_bytes": bytes_read,
            "checksum": f"sha256:{digest.hexdigest()}",
        }

    def file_size(
        self, relative: Any, label: str, *, maximum_bytes: int
    ) -> tuple[str, int]:
        descriptor, pin = self._open_file(relative, label, maximum_bytes)
        try:
            if _identity(os.fstat(descriptor)) != pin.identity:
                raise ValueError(f"{label} changed while it was checked")
        finally:
            os.close(descriptor)
        self._validate_file(pin)
        return pin.relative, pin.identity[3]

    def check_file(
        self, relative: Any, label: str, *, maximum_bytes: int = 1 << 63
    ) -> str:
        canonical, _ = self.file_size(
            relative, label, maximum_bytes=maximum_bytes
        )
        return canonical

    def json(
        self, relative: Any, label: str, *, maximum_bytes: int
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        raw, reference = self.snapshot(
            relative, label, maximum_bytes=maximum_bytes, collect=True
        )
        assert raw is not None
        try:
            value = json.loads(raw.decode("utf-8"), parse_constant=reject_constant)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError(f"{label} is not strict UTF-8 JSON") from error
        return _object(value, label), reference

    def validate(self) -> None:
        reopened, _, _ = _open_absolute_directory(self.path, self.label)
        try:
            if _inode(os.fstat(reopened)) != self._directories[()][1]:
                raise ValueError(f"{self.label} path changed during portal derivation")
        finally:
            os.close(reopened)
        current_names: dict[tuple[str, ...], dict[str, tuple[str, ...]]] = {}
        for parts, (descriptor, inode) in sorted(self._directories.items(), key=lambda item: len(item[0])):
            if _inode(os.fstat(descriptor)) != inode:
                raise ValueError(f"{self.label} directory descriptor changed")
            if parts:
                parent = self._directories[parts[:-1]][0]
                names = current_names.get(parts[:-1])
                if names is None:
                    names = _name_index(self._scan_budget.names(parent, self.label))
                    current_names[parts[:-1]] = names
                _require_exact_name(names, parts[-1], self.label)
                current = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
                if stat.S_ISLNK(current.st_mode) or not stat.S_ISDIR(current.st_mode) or _inode(current) != inode:
                    raise ValueError(f"{self.label} directory path changed")
        for pin in self._files.values():
            names = current_names.get(pin.parent_parts)
            if names is None:
                names = _name_index(
                    self._scan_budget.names(
                        self._directories[pin.parent_parts][0], pin.relative
                    )
                )
                current_names[pin.parent_parts] = names
            _require_exact_name(names, pin.name, pin.relative)
            self._validate_file(pin)


def _root_validation_states(
    roots: tuple[_ConfinedRoot, ...],
) -> tuple[tuple[tuple[str, str, str], ...], Exception | None]:
    states: list[tuple[str, str, str]] = []
    first_error: Exception | None = None
    for root in roots:
        try:
            root.validate()
        except Exception as error:
            states.append(("invalid", type(error).__name__, str(error)))
            first_error = first_error or error
        else:
            states.append(("valid", "", ""))
    return tuple(states), first_error


class _PinnedOutput:
    def __init__(self, out_dir: Path, immutable_roots: list[_ConfinedRoot]) -> None:
        absolute, components = _absolute_parts(out_dir)
        parent_descriptor, parent_path, _ = _open_absolute_directory(
            absolute.parent, "portal derivation output parent"
        )
        self.path = absolute
        self._parent = parent_descriptor
        self._parent_path = parent_path
        self._parent_inode = _inode(os.fstat(parent_descriptor))
        self._name = absolute.name
        self._created_directory = False
        self._closed = False
        self._written = False
        self._directory = -1
        self._report = -1
        self._immutable_roots = tuple(immutable_roots)
        self._expected_root_states: tuple[tuple[str, str, str], ...] | None = None
        self._scan_budget = _DirectoryScanBudget()
        try:
            for root in immutable_roots:
                if (
                    components[: len(root.components)] == root.components
                    or tuple(map(_portable_key, components[: len(root.components)]))
                    == tuple(map(_portable_key, root.components))
                ):
                    raise ValueError(f"portal derivation output must be outside the immutable {root.label}")
            names = self._scan_budget.names(
                parent_descriptor, "portal derivation output parent"
            )
            aliases = [name for name in names if _portable_key(name) == _portable_key(self._name)]
            if aliases and aliases != [self._name]:
                raise ValueError("portal derivation output has a casefold path alias")
            if not aliases:
                os.mkdir(self._name, mode=0o700, dir_fd=parent_descriptor)
                self._created_directory = True
            before = os.stat(self._name, dir_fd=parent_descriptor, follow_symlinks=False)
            if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
                raise ValueError("portal derivation output must be a regular non-symlink directory")
            self._directory = os.open(self._name, _directory_flags(), dir_fd=parent_descriptor)
            opened = os.fstat(self._directory)
            after = os.stat(self._name, dir_fd=parent_descriptor, follow_symlinks=False)
            if _identity(before) != _identity(opened) or _identity(opened) != _identity(after):
                raise ValueError("portal derivation output changed while it was opened")
            self._directory_inode = _inode(opened)
            if any(self._directory_inode == root._directories[()][1] for root in immutable_roots):
                raise ValueError("portal derivation output is an inode alias of an immutable input")
            if self._scan_budget.names(self._directory, "portal derivation output"):
                raise FileExistsError(f"portal derivation output is not empty: {absolute}")
            flags = _file_flags(read_write=True) | os.O_CREAT | os.O_EXCL
            self._report = os.open(REPORT_NAME, flags, 0o600, dir_fd=self._directory)
            report_stat = os.fstat(self._report)
            if not stat.S_ISREG(report_stat.st_mode):
                raise ValueError("portal derivation report reservation is not a regular file")
            self._report_inode = _inode(report_stat)
            self._report_identity = _identity(report_stat)
            self._directory_identity = _identity(os.fstat(self._directory))
            self.validate()
        except Exception:
            self.cleanup()
            self.close()
            raise

    def validate(self) -> None:
        reopened, _, _ = _open_absolute_directory(
            self._parent_path, "portal derivation output parent"
        )
        try:
            if _inode(os.fstat(reopened)) != self._parent_inode:
                raise ValueError("portal derivation output parent path changed during analysis")
        finally:
            os.close(reopened)
        _require_exact_component(
            self._parent,
            self._name,
            "portal derivation output",
            scan_budget=self._scan_budget,
        )
        directory_stat = os.stat(self._name, dir_fd=self._parent, follow_symlinks=False)
        if (
            stat.S_ISLNK(directory_stat.st_mode)
            or not stat.S_ISDIR(directory_stat.st_mode)
            or _inode(directory_stat) != self._directory_inode
        ):
            raise ValueError("portal derivation output path changed during analysis")
        _require_exact_component(
            self._directory,
            REPORT_NAME,
            "portal derivation report",
            scan_budget=self._scan_budget,
        )
        report_stat = os.stat(REPORT_NAME, dir_fd=self._directory, follow_symlinks=False)
        if (
            stat.S_ISLNK(report_stat.st_mode)
            or not stat.S_ISREG(report_stat.st_mode)
            or _identity(report_stat) != self._report_identity
        ):
            raise ValueError("portal derivation report path changed during analysis")
        if _identity(directory_stat) != self._directory_identity:
            raise ValueError("portal derivation output contents changed during analysis")
        if _inode(os.fstat(self._parent)) != self._parent_inode or _inode(os.fstat(self._directory)) != self._directory_inode:
            raise ValueError("portal derivation output descriptor identity changed")

    def bind_root_validation_states(
        self, states: tuple[tuple[str, str, str], ...]
    ) -> None:
        if self._expected_root_states is not None:
            raise RuntimeError("portal derivation root validation states are already bound")
        self._expected_root_states = states

    def validate_context(self) -> None:
        if self._expected_root_states is None:
            raise RuntimeError("portal derivation root validation states are not bound")
        states, _ = _root_validation_states(self._immutable_roots)
        if states != self._expected_root_states:
            raise ValueError(
                "immutable input validation state changed during report publication"
            )
        self.validate()

    def _verify_report_bytes(self, encoded: bytes) -> tuple[int, int, int, int, int, int]:
        before = _identity(os.fstat(self._report))
        os.lseek(self._report, 0, os.SEEK_SET)
        digest = hashlib.sha256()
        offset = 0
        while chunk := os.read(self._report, _READ_CHUNK_BYTES):
            if offset + len(chunk) > len(encoded) or chunk != encoded[offset : offset + len(chunk)]:
                raise ValueError("portal derivation report read-back bytes do not match")
            digest.update(chunk)
            offset += len(chunk)
        after = _identity(os.fstat(self._report))
        if before != after:
            raise ValueError("portal derivation report changed during read-back")
        if offset != len(encoded) or digest.digest() != hashlib.sha256(encoded).digest():
            raise ValueError("portal derivation report read-back digest does not match")
        return after

    def write(self, payload: dict[str, Any]) -> None:
        if self._written:
            raise RuntimeError("portal derivation report can be written only once")
        ensure_finite(payload)
        encoded = (json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")
        if len(encoded) > _MAX_REPORT_BYTES:
            raise ValueError("portal derivation report exceeds the bounded byte limit")
        json.loads(encoded.decode("utf-8"), parse_constant=reject_constant)
        self.validate()
        offset = 0
        while offset < len(encoded):
            written = os.write(self._report, encoded[offset:])
            if written <= 0:
                raise OSError("portal derivation report write made no progress")
            offset += written
        os.fsync(self._report)
        if os.fstat(self._report).st_size != len(encoded):
            raise ValueError("portal derivation report write was incomplete")
        self._report_identity = self._verify_report_bytes(encoded)
        os.fsync(self._directory)
        self.validate()
        self._written = True

    def cleanup(self) -> None:
        if self._directory >= 0 and self._report >= 0:
            try:
                current = os.stat(REPORT_NAME, dir_fd=self._directory, follow_symlinks=False)
                if _inode(current) == getattr(self, "_report_inode", None):
                    os.unlink(REPORT_NAME, dir_fd=self._directory)
            except (FileNotFoundError, OSError):
                pass
        if self._created_directory and self._parent >= 0:
            try:
                current = os.stat(self._name, dir_fd=self._parent, follow_symlinks=False)
                if _inode(current) == getattr(self, "_directory_inode", None):
                    os.rmdir(self._name, dir_fd=self._parent)
            except (FileNotFoundError, OSError):
                pass

    def close(self) -> None:
        if self._closed:
            return
        for descriptor in (self._report, self._directory, self._parent):
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        self._closed = True

    def __enter__(self) -> _PinnedOutput:
        return self

    def __exit__(self, exception_type: object, *_: object) -> None:
        try:
            if self._written:
                try:
                    self.validate_context()
                except Exception:
                    self.cleanup()
                    if exception_type is None:
                        raise
            elif exception_type is not None:
                self.cleanup()
            else:
                self.cleanup()
                raise RuntimeError("portal derivation exited without publishing a report")
        finally:
            self.close()


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{label} must be a finite number")
    return result


def _matrix(value: Any, label: str) -> list[list[float]]:
    if not isinstance(value, list) or len(value) != 4:
        raise ValueError(f"{label} must be a finite rigid 4x4 matrix")
    rows: list[list[float]] = []
    for row in value:
        if not isinstance(row, list) or len(row) != 4:
            raise ValueError(f"{label} must be a finite rigid 4x4 matrix")
        rows.append([_number(item, label) for item in row])
    if any(abs(rows[3][index]) > _MATRIX_TOLERANCE for index in range(3)) or not math.isclose(
        rows[3][3], 1.0, abs_tol=_MATRIX_TOLERANCE
    ):
        raise ValueError(f"{label} homogeneous row is invalid")
    columns = [[rows[row][column] for row in range(3)] for column in range(3)]
    for left in range(3):
        for right in range(3):
            expected = 1.0 if left == right else 0.0
            if not math.isclose(_dot(columns[left], columns[right]), expected, abs_tol=1e-3):
                raise ValueError(f"{label} rotation is not orthonormal")
    determinant = _dot(columns[0], _cross(columns[1], columns[2]))
    if not math.isclose(determinant, 1.0, abs_tol=1e-3):
        raise ValueError(f"{label} rotation determinant is not one")
    return rows


def _dot(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right, strict=True))


def _cross(left: list[float], right: list[float]) -> list[float]:
    return [
        left[1] * right[2] - left[2] * right[1],
        left[2] * right[0] - left[0] * right[2],
        left[0] * right[1] - left[1] * right[0],
    ]


def _center(matrix: list[list[float]]) -> list[float]:
    return [matrix[index][3] for index in range(3)]


def _snapshot(
    path: Path,
    label: str,
    relative: str,
    *,
    collect: bool,
    maximum_bytes: int = _MAX_CAPTURE_JSON_BYTES,
) -> tuple[bytes | None, dict[str, Any]]:
    absolute, _ = _absolute_parts(path)
    with _ConfinedRoot(absolute.parent, f"{label} parent") as root:
        result = root.snapshot(
            absolute.name,
            label,
            maximum_bytes=maximum_bytes,
            collect=collect,
            reference_path=relative,
        )
        root.validate()
        return result


def _canonical_relative_path(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError(f"{label} must be a canonical relative POSIX path")
    declared = PurePosixPath(value)
    windows = PureWindowsPath(value)
    canonical = declared.as_posix()
    if (
        declared.is_absolute()
        or windows.drive
        or ".." in declared.parts
        or canonical == "."
        or canonical != value
        or len(declared.parts) > _MAX_PATH_COMPONENTS
    ):
        raise ValueError(f"{label} must be a canonical relative POSIX path")
    return canonical


def _same_matrix(left: Any, right: Any, label: str) -> None:
    left_matrix = _matrix(left, f"{label} prepared pose")
    right_matrix = _matrix(right, f"{label} source pose")
    if any(
        not math.isclose(left_matrix[row][column], right_matrix[row][column], abs_tol=1e-5)
        for row in range(4)
        for column in range(4)
    ):
        raise ValueError(f"{label} pose does not match its source binding")


def _intrinsics(value: Any, label: str) -> tuple[float, ...]:
    intrinsics = _object(value, label)
    keys = ("fl_x", "fl_y", "cx", "cy", "w", "h")
    result = tuple(_number(intrinsics.get(key), f"{label}.{key}") for key in keys)
    if result[0] <= 0.0 or result[1] <= 0.0 or result[4] <= 0.0 or result[5] <= 0.0:
        raise ValueError(f"{label} focal lengths and dimensions must be positive")
    return result


def _portals(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if payload.get("schema") != "capture_splat.room_semantics.v0.1":
        raise ValueError("RoomPlan semantics schema is unsupported")
    authority = _object(payload.get("authority"), "RoomPlan semantics authority")
    if authority.get("room_semantic_proposal") is not True or any(
        authority.get(key) is not False
        for key in ("metric_authority", "collision_geometry", "planning_authority", "semantic_authority")
    ):
        raise ValueError("RoomPlan semantics must remain a non-authoritative proposal")
    doors = payload.get("doors", [])
    openings = payload.get("openings", [])
    if not isinstance(doors, list) or not isinstance(openings, list) or not doors + openings:
        raise ValueError("RoomPlan semantics contains no door or opening proposals")
    proposals = [("door", value) for value in doors] + [("opening", value) for value in openings]
    if len(proposals) > _MAX_PORTALS:
        raise ValueError("RoomPlan portal proposal count exceeds the bounded work limit")
    portals: list[dict[str, Any]] = []
    ids: set[str] = set()
    for index, (kind, raw) in enumerate(proposals):
        door = _object(raw, f"RoomPlan {kind} {index}")
        portal_id = door.get("id")
        if not isinstance(portal_id, str) or not portal_id or portal_id in ids:
            raise ValueError("RoomPlan door ids must be unique non-empty strings")
        ids.add(portal_id)
        transform = _matrix(door.get("transform_matrix"), f"RoomPlan {kind} {portal_id}")
        dimensions = _object(door.get("dimensions_meters"), f"RoomPlan {kind} {portal_id} dimensions")
        width = _number(dimensions.get("x"), f"RoomPlan door {portal_id} width")
        height = _number(dimensions.get("y"), f"RoomPlan door {portal_id} height")
        if width <= 0.0 or height <= 0.0:
            raise ValueError("RoomPlan door dimensions must be positive")
        width_axis = [transform[row][0] for row in range(3)]
        vertical_axis = [transform[row][1] for row in range(3)]
        normal = [transform[row][2] for row in range(3)]
        if abs(_dot(vertical_axis, [0.0, 1.0, 0.0])) < 0.99 or abs(normal[1]) > 0.01:
            raise ValueError("RoomPlan door proposal is not vertical in ARKit world")
        portals.append(
            {
                "id": portal_id,
                "kind": kind,
                "center": _center(transform),
                "width_axis": width_axis,
                "vertical_axis": vertical_axis,
                "normal": normal,
                "width_meters": width,
                "height_meters": height,
                "crossing_count": 0,
                "crossings": [],
                "rejected_crossing_count": 0,
                "rejected_crossings": [],
            }
        )
    return portals


def _signed_distance(point: list[float], portal: dict[str, Any]) -> float:
    return _dot([point[index] - portal["center"][index] for index in range(3)], portal["normal"])


def _inside_portal(point: list[float], portal: dict[str, Any]) -> bool:
    relative = [point[index] - portal["center"][index] for index in range(3)]
    return (
        abs(_dot(relative, portal["width_axis"])) <= portal["width_meters"] / 2.0
        and abs(_dot(relative, portal["vertical_axis"])) <= portal["height_meters"] / 2.0
    )


def _crossing(
    previous: dict[str, Any], current: dict[str, Any], portal: dict[str, Any]
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    start = _signed_distance(previous["position"], portal)
    end = _signed_distance(current["position"], portal)
    if not ((start < 0.0 < end) or (end < 0.0 < start)):
        return None, None
    fraction = start / (start - end)
    position = [
        previous["position"][index]
        + fraction * (current["position"][index] - previous["position"][index])
        for index in range(3)
    ]
    if not _inside_portal(position, portal):
        return None, None
    delta_seconds = current["timestamp"] - previous["timestamp"]
    distance_meters = math.dist(previous["position"], current["position"])
    speed = distance_meters / delta_seconds
    event = {
        "from_video_frame": previous["video_frame"],
        "to_video_frame": current["video_frame"],
        "from_timestamp": previous["timestamp"],
        "to_timestamp": current["timestamp"],
        "delta_seconds": delta_seconds,
        "distance_meters": distance_meters,
        "speed_meters_per_second": speed,
        "from_tracking_state": previous["tracking_state"],
        "to_tracking_state": current["tracking_state"],
        "position_meters": position,
        "direction": "side_a_to_side_b" if start < end else "side_b_to_side_a",
    }
    reasons: list[str] = []
    if current["video_frame"] != previous["video_frame"] + 1:
        reasons.append("video_frame_gap")
    if previous["tracking_state"] != "normal" or current["tracking_state"] != "normal":
        reasons.append("tracking_not_normal")
    if delta_seconds > _MAX_CROSSING_DELTA_SECONDS:
        reasons.append("timestamp_gap_exceeds_limit")
    if distance_meters > _MAX_CROSSING_DISTANCE_METERS:
        reasons.append("translation_exceeds_limit")
    if speed > _MAX_CROSSING_SPEED_METERS_PER_SECOND:
        reasons.append("speed_exceeds_limit")
    if reasons:
        return None, {**event, "reasons": reasons}
    return event, None


def _prepared_frames(
    root: _ConfinedRoot, prepared: dict[str, Any], source: dict[str, Any]
) -> tuple[
    list[dict[str, Any]],
    dict[int, dict[str, Any]],
    dict[str, int],
    dict[str, str],
]:
    if prepared.get("schema") != "capture_splat.v0.3" or prepared.get("source") != "capture_splat.prepare_capture":
        raise ValueError("prepared capture must use capture_splat.v0.3 prepare_capture schema")
    frames = prepared.get("frames")
    source_frames = source.get("frames")
    if not isinstance(frames, list) or not isinstance(source_frames, list):
        raise ValueError("prepared and source capture frames must be arrays")
    if len(frames) > _MAX_PREPARED_FRAMES or len(source_frames) > _MAX_PREPARED_FRAMES:
        raise ValueError("prepared or source frame count exceeds the bounded record limit")
    video_bindings: dict[int, dict[str, Any]] = {}
    source_indices: set[int] = set()
    prepared_images: dict[str, str] = {}
    counts = {"continuous_video": 0, "accepted_rgbd": 0}
    for index, raw in enumerate(frames):
        frame = _object(raw, f"prepared frame {index}")
        if frame.get("accepted") is not True:
            raise ValueError("prepared portal analysis accepts only retained frames")
        kind = frame.get("source_kind")
        if kind not in counts:
            raise ValueError(f"prepared frame {index} has unsupported source_kind")
        counts[kind] += 1
        _matrix(frame.get("transform_matrix"), f"prepared frame {index} pose")
        _number(frame.get("timestamp"), f"prepared frame {index} timestamp")
        _intrinsics(frame.get("intrinsics"), f"prepared frame {index} intrinsics")
        rgb_relative = _canonical_relative_path(frame.get("rgb"), f"prepared frame {index} RGB")
        try:
            image_name = PurePosixPath(rgb_relative).relative_to("images").as_posix()
        except ValueError as error:
            raise ValueError("prepared RGB must be below the prepared images directory") from error
        rgb_path = root.check_file(rgb_relative, f"prepared frame {index} RGB")
        if image_name in prepared_images:
            raise ValueError("prepared RGB paths are duplicated")
        prepared_images[image_name] = rgb_path
        if kind == "continuous_video":
            source_video_frame = frame.get("source_video_frame")
            if (
                isinstance(source_video_frame, bool)
                or not isinstance(source_video_frame, int)
                or source_video_frame < 0
            ):
                raise ValueError("continuous-video frame source index is invalid")
            if source_video_frame in video_bindings:
                raise ValueError("continuous-video frame source index is duplicated")
            video_bindings[source_video_frame] = frame
            continue
        source_index = frame.get("source_frame_index")
        if (
            isinstance(source_index, bool)
            or not isinstance(source_index, int)
            or not 1 <= source_index <= len(source_frames)
        ):
            raise ValueError("accepted RGB-D source frame index is invalid")
        if source_index in source_indices:
            raise ValueError("accepted RGB-D source frame index is duplicated")
        source_indices.add(source_index)
        source_frame = _object(source_frames[source_index - 1], f"source frame {source_index}")
        source_quality = _object(
            source_frame.get("capture_quality"), f"source frame {source_index} quality"
        )
        if source_quality.get("accepted") is not True:
            raise ValueError("accepted RGB-D frame is not accepted by the source capture")
        if not math.isclose(
            _number(frame.get("timestamp"), "prepared RGB-D timestamp"),
            _number(source_frame.get("timestamp"), "source RGB-D timestamp"),
            abs_tol=1e-6,
        ):
            raise ValueError("accepted RGB-D timestamp does not match its source binding")
        _same_matrix(frame.get("transform_matrix"), source_frame.get("transform_matrix"), "accepted RGB-D")
        if _intrinsics(frame.get("intrinsics"), "prepared RGB-D intrinsics") != _intrinsics(
            source_frame.get("intrinsics"), "source RGB-D intrinsics"
        ):
            raise ValueError("accepted RGB-D intrinsics do not match its source binding")
        root.check_file(frame.get("depth"), f"prepared frame {index} depth")
        root.check_file(frame.get("confidence"), f"prepared frame {index} confidence")
    return frames, video_bindings, counts, prepared_images


def _trajectory(
    root: _ConfinedRoot,
    relative: str,
    expected_sample_count: int,
    portals: list[dict[str, Any]],
    video_bindings: dict[int, dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    descriptor, pin = root._open_file(relative, "full trajectory", _MAX_TRAJECTORY_BYTES)
    digest = hashlib.sha256()
    previous: dict[str, Any] | None = None
    matched: set[int] = set()
    sample_count = 0
    bytes_read = 0
    normal_tracking_samples = 0
    accepted_crossing_count = 0
    rejected_crossing_count = 0
    retained_crossing_count = 0
    first: dict[str, Any] | None = None
    last: dict[str, Any] | None = None
    try:
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            line_number = 0
            while raw_line := handle.readline(_MAX_TRAJECTORY_LINE_BYTES + 1):
                line_number += 1
                if len(raw_line) > _MAX_TRAJECTORY_LINE_BYTES:
                    raise ValueError("trajectory line exceeds the bounded byte limit")
                digest.update(raw_line)
                bytes_read += len(raw_line)
                if bytes_read > _MAX_TRAJECTORY_BYTES:
                    raise ValueError("full trajectory exceeds the bounded byte limit")
                try:
                    line = raw_line.decode("utf-8")
                except UnicodeDecodeError as error:
                    raise ValueError("full trajectory is not strict UTF-8 JSONL") from error
                if not line.strip():
                    continue
                sample_count += 1
                if (
                    sample_count > _MAX_TRAJECTORY_SAMPLES
                    or sample_count * len(portals) > _MAX_DISTANCE_EVALUATIONS
                ):
                    raise ValueError("trajectory analysis exceeds the bounded work limit")
                try:
                    value = json.loads(line, parse_constant=reject_constant)
                except json.JSONDecodeError as error:
                    raise ValueError(f"trajectory line {line_number} is invalid JSON") from error
                sample = _object(value, f"trajectory line {line_number}")
                video_frame = sample.get("video_frame_idx")
                expected_video_frame = sample_count - 1
                if (
                    isinstance(video_frame, bool)
                    or not isinstance(video_frame, int)
                    or video_frame != expected_video_frame
                ):
                    raise ValueError("full trajectory video_frame_idx must be exactly 0..N-1")
                timestamp = _number(sample.get("ar_timestamp"), "trajectory ar_timestamp")
                pose = _matrix(sample.get("camera_to_world"), "trajectory camera_to_world")
                tracking_state = sample.get("tracking_state")
                if not isinstance(tracking_state, str) or not tracking_state:
                    raise ValueError("trajectory tracking_state must be a non-empty string")
                normal_tracking_samples += int(tracking_state == "normal")
                current = {
                    "video_frame": video_frame,
                    "timestamp": timestamp,
                    "position": _center(pose),
                    "tracking_state": tracking_state,
                }
                if previous is not None and timestamp <= previous["timestamp"]:
                    raise ValueError("full trajectory timestamps are not strictly ordered")
                binding = video_bindings.get(video_frame)
                if binding is not None:
                    if not math.isclose(
                        timestamp,
                        _number(binding.get("timestamp"), "prepared video timestamp"),
                        abs_tol=1e-6,
                    ):
                        raise ValueError("prepared video timestamp does not match the full trajectory")
                    _same_matrix(binding.get("transform_matrix"), pose, "continuous-video frame")
                    if _intrinsics(
                        binding.get("intrinsics"), "prepared video intrinsics"
                    ) != _intrinsics(sample.get("intrinsics"), "trajectory video intrinsics"):
                        raise ValueError("prepared video intrinsics do not match the full trajectory")
                    matched.add(video_frame)
                if previous is not None:
                    for portal in portals:
                        event, rejected = _crossing(previous, current, portal)
                        if event is not None:
                            accepted_crossing_count += 1
                            portal["crossing_count"] += 1
                            if retained_crossing_count < _MAX_RETAINED_CROSSING_EVENTS:
                                portal["crossings"].append(event)
                                retained_crossing_count += 1
                        if rejected is not None:
                            rejected_crossing_count += 1
                            portal["rejected_crossing_count"] += 1
                            if retained_crossing_count < _MAX_RETAINED_CROSSING_EVENTS:
                                portal["rejected_crossings"].append(rejected)
                                retained_crossing_count += 1
                first = first or current
                last = current
                previous = current
        after_open = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if _identity(after_open) != pin.identity or bytes_read != pin.identity[3]:
        raise ValueError("full trajectory changed while it was read")
    root._validate_file(pin)
    if sample_count != expected_sample_count:
        raise ValueError("full trajectory sample count does not match source video_frame_count")
    if matched != set(video_bindings):
        raise ValueError("full trajectory does not bind every prepared continuous-video frame")
    assert first is not None and last is not None
    report = {
        "sample_count": sample_count,
        "source_video_frame_count": expected_sample_count,
        "first_video_frame": first["video_frame"],
        "last_video_frame": last["video_frame"],
        "first_timestamp": first["timestamp"],
        "last_timestamp": last["timestamp"],
        "prepared_video_bindings": len(matched),
        "normal_tracking_samples": normal_tracking_samples,
        "non_normal_tracking_samples": sample_count - normal_tracking_samples,
        "index_contract": "exact_contiguous_0_to_source_video_frame_count_minus_1",
        "crossing_bracket_limits": {
            "tracking_state": "normal_on_both_samples",
            "maximum_delta_seconds": _MAX_CROSSING_DELTA_SECONDS,
            "maximum_distance_meters": _MAX_CROSSING_DISTANCE_METERS,
            "maximum_speed_meters_per_second": _MAX_CROSSING_SPEED_METERS_PER_SECOND,
        },
        "crossing_event_retention": {
            "accepted_total": accepted_crossing_count,
            "rejected_total": rejected_crossing_count,
            "retained_total": retained_crossing_count,
            "omitted_total": accepted_crossing_count
            + rejected_crossing_count
            - retained_crossing_count,
            "maximum_retained": _MAX_RETAINED_CROSSING_EVENTS,
        },
    }
    reference = {
        "path": relative,
        "size_bytes": bytes_read,
        "checksum": f"sha256:{digest.hexdigest()}",
    }
    return report, reference


def _select_portal(portals: list[dict[str, Any]], requested: str | None) -> tuple[dict[str, Any] | None, str]:
    requested_portal: dict[str, Any] | None = None
    if requested is not None:
        matches = [portal for portal in portals if portal["id"] == requested]
        if not matches:
            raise ValueError(f"requested RoomPlan portal is missing: {requested}")
        requested_portal = matches[0]
    crossed = [portal for portal in portals if portal["crossing_count"] > 0]
    if len(crossed) != 1:
        suffix = "missing" if not crossed else "ambiguous"
        return None, f"requested_crossing_{suffix}" if requested is not None else suffix
    observed = crossed[0]
    if requested_portal is not None and observed is not requested_portal:
        return None, "requested_crossing_mismatch"
    return observed, (
        "requested_unique_observed_crossing"
        if requested is not None
        else "unique_observed_crossing"
    )


def _region(point: list[float], portal: dict[str, Any], through_band: float) -> str:
    signed = _signed_distance(point, portal)
    if signed < -through_band:
        return "side_a"
    if signed > through_band:
        return "side_b"
    return "through_opening" if _inside_portal(point, portal) else "outside_portal_band"


def _registered_image_names(raw: bytes) -> tuple[list[str], int]:
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeDecodeError as error:
        raise ValueError("COLMAP images.txt is not UTF-8") from error
    names: list[str] = []
    image_ids: set[int] = set()
    invalid_records = 0
    expect_pose = True
    for raw_line in lines:
        stripped = raw_line.strip()
        if stripped.startswith("#"):
            continue
        if not expect_pose:
            expect_pose = True
            if stripped:
                points = stripped.split()
                try:
                    if len(points) % 3:
                        raise ValueError
                    for index in range(0, len(points), 3):
                        coordinates = (float(points[index]), float(points[index + 1]))
                        if not all(math.isfinite(value) for value in coordinates):
                            raise ValueError
                        int(points[index + 2])
                except ValueError:
                    invalid_records += 1
            continue
        if not stripped:
            continue
        parts = stripped.split(maxsplit=9)
        try:
            if len(parts) < 10:
                raise ValueError
            image_id = int(parts[0])
            if image_id <= 0 or image_id in image_ids:
                raise ValueError
            pose = [float(value) for value in parts[1:8]]
            if not all(math.isfinite(value) for value in pose):
                raise ValueError
            quaternion_norm = math.sqrt(sum(value * value for value in pose[:4]))
            if not math.isclose(
                quaternion_norm, 1.0, abs_tol=_COLMAP_QUATERNION_TOLERANCE
            ):
                raise ValueError
            camera_id = int(parts[8])
            if camera_id <= 0:
                raise ValueError
        except ValueError:
            invalid_records += 1
            continue
        if len(names) >= _MAX_COLMAP_IMAGE_RECORDS:
            raise ValueError("COLMAP images.txt exceeds the bounded image record limit")
        image_ids.add(image_id)
        names.append(parts[9])
        expect_pose = False
    if not expect_pose:
        invalid_records += 1
    return names, invalid_records


def _registration(
    sfm_root: _ConfinedRoot | None,
    prepared_root: _ConfinedRoot,
    prepared_images: dict[str, str],
) -> tuple[dict[str, Any], set[str]]:
    if sfm_root is None:
        return {
            "supplied": False,
            "reason": "colmap_registration_missing",
            "metric_roomplan_registration": False,
        }, set()
    sfm_root._directory(("images",), "SfM image root")
    raw, images_ref = sfm_root.snapshot(
        "sparse/0/images.txt",
        "COLMAP images.txt",
        maximum_bytes=_MAX_COLMAP_IMAGES_BYTES,
        collect=True,
    )
    assert raw is not None
    names, invalid = _registered_image_names(raw)
    if invalid or len(names) != len(set(names)):
        raise ValueError("COLMAP images.txt registration records are invalid or duplicated")
    canonical_names = [
        _canonical_relative_path(name, "COLMAP registered image name") for name in names
    ]
    if len(canonical_names) != len(set(canonical_names)) or len(canonical_names) != len(
        {_portable_key(name) for name in canonical_names}
    ):
        raise ValueError("COLMAP registered image paths are duplicated or casefold aliases")
    registered_prepared: set[str] = set()
    parity_records: list[tuple[str, int, str]] = []
    parity_combined_bytes = 0
    for name in canonical_names:
        sfm_relative = f"images/{name}"
        prepared_image = prepared_images.get(name)
        if prepared_image is None:
            sfm_root.check_file(sfm_relative, f"registered SfM image {name}")
            continue
        _, sfm_size = sfm_root.file_size(
            sfm_relative,
            f"registered SfM image {name}",
            maximum_bytes=_MAX_PARITY_IMAGE_BYTES,
        )
        _, prepared_size = prepared_root.file_size(
            prepared_image,
            f"prepared image matching {name}",
            maximum_bytes=_MAX_PARITY_IMAGE_BYTES,
        )
        if sfm_size != prepared_size:
            raise ValueError(
                f"registered SfM image size does not match prepared RGB: {name}"
            )
        if (
            sfm_root._files[sfm_relative].identity[:2]
            == prepared_root._files[prepared_image].identity[:2]
        ):
            raise ValueError(
                f"registered SfM image is an inode alias of prepared RGB: {name}"
            )
        pair_bytes = sfm_size + prepared_size
        if parity_combined_bytes + pair_bytes > _MAX_PARITY_COMBINED_BYTES:
            raise ValueError("registered image parity exceeds the aggregate byte limit")
        parity_combined_bytes += pair_bytes
        _, sfm_ref = sfm_root.snapshot(
            sfm_relative,
            f"registered SfM image {name}",
            maximum_bytes=_MAX_PARITY_IMAGE_BYTES,
            collect=False,
            reference_path=name,
        )
        _, prepared_ref = prepared_root.snapshot(
            prepared_image,
            f"prepared image matching {name}",
            maximum_bytes=_MAX_PARITY_IMAGE_BYTES,
            collect=False,
            reference_path=name,
        )
        if any(sfm_ref[key] != prepared_ref[key] for key in ("size_bytes", "checksum")):
            raise ValueError(f"registered SfM image bytes do not match prepared RGB: {name}")
        registered_prepared.add(name)
        parity_records.append((name, sfm_ref["size_bytes"], sfm_ref["checksum"]))
    parity_digest = hashlib.sha256()
    for name, size, checksum in sorted(parity_records):
        parity_digest.update(name.encode("utf-8"))
        parity_digest.update(b"\0")
        parity_digest.update(str(size).encode("ascii"))
        parity_digest.update(b"\0")
        parity_digest.update(checksum.encode("ascii"))
        parity_digest.update(b"\n")
    return {
        "supplied": True,
        "sfm_package": {
            "path": sfm_root.path.name,
            "layout": "images_and_sparse_0_images_txt",
        },
        "images_txt": images_ref,
        "image_root": "images",
        "registered_image_count": len(names),
        "registered_prepared_image_count": len(registered_prepared),
        "registered_prepared_image_parity": {
            "count": len(parity_records),
            "digest": f"sha256:{parity_digest.hexdigest()}",
            "canonicalization": "utf8_relative_path_nul_size_nul_sha256_lf_v1",
            "sfm_bytes_hashed": parity_combined_bytes // 2,
            "prepared_bytes_hashed": parity_combined_bytes // 2,
            "combined_bytes_hashed": parity_combined_bytes,
            "maximum_image_bytes": _MAX_PARITY_IMAGE_BYTES,
            "maximum_combined_bytes": _MAX_PARITY_COMBINED_BYTES,
            "comparison_order": "canonical_path_then_size_then_sha256",
        },
        "matching": "canonical_case_sensitive_relative_path_with_exact_size_and_sha256_parity",
        "metric_roomplan_registration": False,
    }, registered_prepared


def _derive(
    root: _ConfinedRoot,
    capture_relative: str,
    *,
    sfm_package: _ConfinedRoot | None,
    portal_id: str | None,
    through_band_meters: float,
) -> dict[str, Any]:
    through_band = _number(through_band_meters, "through band")
    if through_band <= 0.0:
        raise ValueError("through band must be positive")
    prepared, prepared_ref = root.json(
        capture_relative, "prepared capture", maximum_bytes=_MAX_CAPTURE_JSON_BYTES
    )
    source, source_ref = root.json(
        prepared.get("source_capture_manifest_file"),
        "source capture manifest",
        maximum_bytes=_MAX_CAPTURE_JSON_BYTES,
    )
    trajectory_relative = _canonical_relative_path(
        prepared.get("frame_index_file"), "prepared full trajectory"
    )
    semantics, semantics_ref = root.json(
        prepared.get("room_plan_semantics_file"),
        "RoomPlan semantics",
        maximum_bytes=_MAX_AUXILIARY_JSON_BYTES,
    )
    _, roomplan_ref = root.snapshot(
        prepared.get("room_plan_file"),
        "RoomPlan USDZ",
        maximum_bytes=512 * 1024 * 1024,
        collect=False,
    )
    roomplan_report, roomplan_report_ref = root.json(
        prepared.get("room_plan_report_file"),
        "RoomPlan report",
        maximum_bytes=_MAX_AUXILIARY_JSON_BYTES,
    )
    if source.get("schema") != "capture_splat.v0.3":
        raise ValueError("source capture schema is unsupported")
    if source.get("frame_index_file") != trajectory_relative:
        raise ValueError("source and prepared trajectory references do not match")
    expected_sample_count = source.get("video_frame_count")
    if (
        isinstance(expected_sample_count, bool)
        or not isinstance(expected_sample_count, int)
        or not 2 <= expected_sample_count <= _MAX_TRAJECTORY_SAMPLES
    ):
        raise ValueError("source video_frame_count is invalid or exceeds the bounded work limit")
    if (
        roomplan_report.get("schema") != "capture_splat.room_plan_report.v0.1"
        or roomplan_report.get("room_plan_file") != prepared.get("room_plan_file")
        or roomplan_report.get("room_semantics_file") != prepared.get("room_plan_semantics_file")
        or roomplan_report.get("doors") != len(semantics.get("doors", []))
        or roomplan_report.get("openings") != len(semantics.get("openings", []))
    ):
        raise ValueError("RoomPlan report does not bind the prepared RoomPlan proposal")
    portals = _portals(semantics)
    frames, video_bindings, source_counts, prepared_images = _prepared_frames(
        root, prepared, source
    )
    trajectory, trajectory_ref = _trajectory(
        root,
        trajectory_relative,
        expected_sample_count,
        portals,
        video_bindings,
    )
    selected, selection = _select_portal(portals, portal_id)
    registration, registered = _registration(sfm_package, root, prepared_images)

    prepared_counts = {region: 0 for region in (*_REGIONS, "outside_portal_band")}
    rgbd_counts = {region: 0 for region in _REGIONS}
    registered_rgbd_counts = {region: 0 for region in _REGIONS}
    if selected is not None:
        for frame in frames:
            pose = _matrix(frame.get("transform_matrix"), "prepared frame pose")
            region = _region(_center(pose), selected, through_band)
            prepared_counts[region] += 1
            if frame["source_kind"] != "accepted_rgbd" or region not in rgbd_counts:
                continue
            rgbd_counts[region] += 1
            image_name = PurePosixPath(frame["rgb"]).relative_to("images").as_posix()
            if image_name in registered:
                registered_rgbd_counts[region] += 1

    hold_reasons: list[str] = ["registered_roomplan_missing"]
    if selected is None:
        hold_reasons.append(f"portal_selection_{selection}")
    elif selected["crossing_count"] == 0:
        hold_reasons.append("trajectory_portal_crossing_missing")
    if any(portal["rejected_crossing_count"] for portal in portals):
        hold_reasons.append("trajectory_portal_crossing_bracket_invalid")
    for region in _REGIONS:
        if rgbd_counts[region] == 0:
            hold_reasons.append(f"accepted_rgbd_{region}_missing")
    if not registration["supplied"]:
        hold_reasons.append("colmap_registration_missing")
    else:
        for region in _REGIONS:
            if registered_rgbd_counts[region] == 0:
                hold_reasons.append(f"registered_rgbd_{region}_missing")
    hold_reasons.extend(
        ["observed_free_space_missing", "route_corridor_missing", "prior_closed_state_control_missing"]
    )
    portal_summaries = [
        {
            "id": portal["id"],
            "kind": portal["kind"],
            "width_meters": portal["width_meters"],
            "height_meters": portal["height_meters"],
            "crossing_count": portal["crossing_count"],
            "retained_crossing_count": len(portal["crossings"]),
            "crossings": portal["crossings"],
            "rejected_crossing_count": portal["rejected_crossing_count"],
            "retained_rejected_crossing_count": len(portal["rejected_crossings"]),
            "rejected_crossings": portal["rejected_crossings"],
        }
        for portal in portals
    ]
    return {
        "schema": REPORT_SCHEMA,
        "status": "held_missing_evidence",
        "decision": "hold",
        "reason": hold_reasons[0],
        "hold_reasons": hold_reasons,
        "inputs": {
            "prepared_capture": prepared_ref,
            "source_capture": source_ref,
            "full_trajectory": trajectory_ref,
            "roomplan_semantics": semantics_ref,
            "roomplan_usdz": roomplan_ref,
            "roomplan_report": roomplan_report_ref,
            "sfm_package": registration.get("sfm_package"),
            "colmap_images": registration.get("images_txt"),
        },
        "coordinate_contract": {
            "frame": "arkit_world_shared_session_proposal",
            "units": "meters",
            "roomplan_registration": "missing",
            "shared_session_events": "not_packaged_by_prepare_capture",
            "through_band_meters": through_band,
        },
        "portal_analysis": {
            "selection": selection,
            "requested_portal_id": portal_id,
            "selected_portal_id": selected["id"] if selected is not None else None,
            "candidates": portal_summaries,
        },
        "trajectory": trajectory,
        "frame_bindings": {
            "prepared_frame_count": len(frames),
            "source_capture_frame_count": len(source.get("frames", [])),
            "source_kind_counts": source_counts,
            "prepared_region_counts": prepared_counts,
            "accepted_rgbd_region_counts": rgbd_counts,
            "registered_accepted_rgbd_region_counts": registered_rgbd_counts,
            "synthetic_rgbd_generated": False,
            "rgbd_source": "prepared_accepted_rgbd_with_existing_depth_and_confidence_only",
        },
        "colmap_registration": registration,
        "rails": {
            "roomplan_geometry": "accepted_proposal_only",
            "full_trajectory": "accepted_exact_count_contiguous_source_evidence",
            "prepared_source_frame_bindings": "accepted_pose_timestamp_intrinsics_and_prepared_asset_presence",
            "trajectory_portal_crossing": (
                "observed_proposal_only"
                if selected and selected["crossing_count"] > 0
                else "held_missing"
            ),
            "accepted_rgbd_both_sides_and_through": (
                "held_missing"
                if any(value == 0 for value in rgbd_counts.values())
                else "accepted_capture_evidence_only"
            ),
            "registered_rgbd_both_sides_and_through": (
                "held_missing"
                if any(value == 0 for value in registered_rgbd_counts.values())
                else "accepted_registration_evidence_only"
            ),
            "free_space": "held_missing",
            "route_corridor": "held_missing",
            "prior_closed_state_control": "held_missing",
            "source_asset_byte_parity": "unavailable_source_assets_not_in_prepared_package",
        },
        "outcome": {
            "producer_contract_valid": False,
            "evidence_complete_for_future_reduction_design": False,
            "reduction_started": False,
            "traversable": False,
            "collision_candidate_promoted": False,
        },
        "authority": _false_authority(),
    }


def derive_portal_route_evidence(
    prepared_capture: Path,
    out_dir: Path,
    *,
    sfm_package: Path | None = None,
    portal_id: str | None = None,
    through_band_meters: float = DEFAULT_THROUGH_BAND_METERS,
) -> dict[str, Any]:
    capture_absolute, _ = _absolute_parts(prepared_capture)
    prepared_root = _ConfinedRoot(capture_absolute.parent, "prepared capture")
    sfm_root: _ConfinedRoot | None = None
    try:
        if sfm_package is not None:
            sfm_root = _ConfinedRoot(sfm_package, "SfM package")
            if (
                sfm_root._directories[()][1] == prepared_root._directories[()][1]
                or tuple(map(_portable_key, sfm_root.components))
                == tuple(map(_portable_key, prepared_root.components))
            ):
                raise ValueError("SfM package is an inode or casefold alias of the prepared capture")
        immutable_roots = [prepared_root, *([sfm_root] if sfm_root is not None else [])]
        with _PinnedOutput(out_dir, immutable_roots) as output:
            root_states: tuple[tuple[str, str, str], ...] | None = None
            root_error: Exception | None = None
            try:
                report = _derive(
                    prepared_root,
                    capture_absolute.name,
                    sfm_package=sfm_root,
                    portal_id=portal_id,
                    through_band_meters=through_band_meters,
                )
                root_states, root_error = _root_validation_states(
                    tuple(immutable_roots)
                )
                if root_error is not None:
                    raise root_error
            except Exception as error:
                if root_states is None:
                    root_states, validation_error = _root_validation_states(
                        tuple(immutable_roots)
                    )
                else:
                    validation_error = root_error
                failure = validation_error or error
                output.bind_root_validation_states(root_states)
                rejected = {
                    "schema": REPORT_SCHEMA,
                    "status": "rejected",
                    "decision": "reject",
                    "reason": "portal_route_derivation_failed",
                    "error": str(failure),
                    "error_type": type(failure).__name__,
                    "authority": _false_authority(),
                }
                output.write(rejected)
                output.validate_context()
                if failure is error:
                    raise
                raise failure from error
            output.bind_root_validation_states(root_states)
            output.write(report)
            return report
    finally:
        if sfm_root is not None:
            sfm_root.close()
        prepared_root.close()
