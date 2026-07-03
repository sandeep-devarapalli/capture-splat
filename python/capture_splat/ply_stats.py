from __future__ import annotations

import math
import struct
from pathlib import Path
from typing import Any, BinaryIO

import numpy as np

from .json_utils import write_json_strict

PLY_TYPES = {
    "char": ("b", 1, int),
    "int8": ("b", 1, int),
    "uchar": ("B", 1, int),
    "uint8": ("B", 1, int),
    "short": ("h", 2, int),
    "int16": ("h", 2, int),
    "ushort": ("H", 2, int),
    "uint16": ("H", 2, int),
    "int": ("i", 4, int),
    "int32": ("i", 4, int),
    "uint": ("I", 4, int),
    "uint32": ("I", 4, int),
    "float": ("f", 4, float),
    "float32": ("f", 4, float),
    "double": ("d", 8, float),
    "float64": ("d", 8, float),
}


def _parse_header(handle: BinaryIO) -> tuple[dict[str, Any], int]:
    lines: list[str] = []
    while True:
        raw = handle.readline()
        if raw == b"":
            raise ValueError("PLY header ended before end_header")
        try:
            line = raw.decode("ascii").strip()
        except UnicodeDecodeError as exc:
            raise ValueError("PLY header is not ASCII") from exc
        lines.append(line)
        if line == "end_header":
            break
    if not lines or lines[0] != "ply":
        raise ValueError("not a PLY file")
    fmt = None
    elements: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in lines[1:]:
        if not line or line.startswith("comment"):
            continue
        parts = line.split()
        if parts[0] == "format":
            fmt = parts[1]
        elif parts[0] == "element":
            current = {"name": parts[1], "count": int(parts[2]), "properties": []}
            elements.append(current)
        elif parts[0] == "property" and current is not None:
            if parts[1] == "list":
                current["properties"].append({"kind": "list", "count_type": parts[2], "value_type": parts[3], "name": parts[4]})
            else:
                current["properties"].append({"kind": "scalar", "type": parts[1], "name": parts[2]})
    if fmt not in {"ascii", "binary_little_endian", "binary_big_endian"}:
        raise ValueError(f"unsupported PLY format: {fmt}")
    return {"format": fmt, "elements": elements}, handle.tell()


def _stats(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"count": 0, "mean": None, "min": None, "max": None}
    return {
        "count": len(values),
        "mean": float(np.mean(values)),
        "min": float(np.min(values)),
        "max": float(np.max(values)),
    }


def _read_binary_scalar(handle: BinaryIO, endian: str, data_type: str) -> float | int:
    if data_type not in PLY_TYPES:
        raise ValueError(f"unsupported PLY property type: {data_type}")
    fmt, size, caster = PLY_TYPES[data_type]
    raw = handle.read(size)
    if len(raw) != size:
        raise ValueError("PLY binary data ended early")
    return caster(struct.unpack(endian + fmt, raw)[0])


def _inspect_ascii(lines: list[str], vertex_count: int, vertex_properties: list[dict[str, str]]) -> tuple[int, dict[str, list[float]]]:
    non_finite = 0
    scales = {name: [] for name in ("scale_0", "scale_1", "scale_2")}
    numeric_indexes = [index for index, prop in enumerate(vertex_properties) if prop["kind"] == "scalar" and prop["type"] in PLY_TYPES]
    scale_indexes = {prop["name"]: index for index, prop in enumerate(vertex_properties) if prop["name"] in scales}
    for row_index, line in enumerate(lines[:vertex_count], start=1):
        parts = line.split()
        if len(parts) < len(vertex_properties):
            raise ValueError(f"vertex row {row_index} has too few columns")
        for index in numeric_indexes:
            value = float(parts[index])
            if not math.isfinite(value):
                non_finite += 1
        for name, index in scale_indexes.items():
            value = float(parts[index])
            if math.isfinite(value):
                scales[name].append(value)
    return non_finite, scales


def _inspect_binary(handle: BinaryIO, header: dict[str, Any], vertex_count: int, vertex_properties: list[dict[str, str]]) -> tuple[int, dict[str, list[float]]]:
    endian = "<" if header["format"] == "binary_little_endian" else ">"
    non_finite = 0
    scales = {name: [] for name in ("scale_0", "scale_1", "scale_2")}
    for _ in range(vertex_count):
        for prop in vertex_properties:
            if prop["kind"] == "list":
                count = int(_read_binary_scalar(handle, endian, prop["count_type"]))
                for _ in range(count):
                    _read_binary_scalar(handle, endian, prop["value_type"])
                continue
            value = _read_binary_scalar(handle, endian, prop["type"])
            if isinstance(value, float) and not math.isfinite(value):
                non_finite += 1
            if prop["name"] in scales and isinstance(value, float) and math.isfinite(value):
                scales[prop["name"]].append(value)
    return non_finite, scales


def inspect_ply(path: Path) -> dict[str, Any]:
    path = path.resolve()
    with path.open("rb") as handle:
        header, data_offset = _parse_header(handle)
        vertex_element = next((element for element in header["elements"] if element["name"] == "vertex"), None)
        if vertex_element is None:
            raise ValueError("PLY has no vertex element")
        vertex_count = int(vertex_element["count"])
        vertex_properties = vertex_element["properties"]
        if header["format"] == "ascii":
            text = path.read_text(encoding="ascii")
            body = text[text.index("end_header") + len("end_header"):].strip().splitlines()
            non_finite, scales = _inspect_ascii(body, vertex_count, vertex_properties)
        else:
            handle.seek(data_offset)
            non_finite, scales = _inspect_binary(handle, header, vertex_count, vertex_properties)

    scale_summary = {name: _stats(values) for name, values in scales.items()}
    radius_values = []
    for values in scales.values():
        radius_values.extend([math.exp(value) for value in values if value < 80.0])
    radius_summary = _stats(radius_values)
    summary = {
        "schema": "capture_splat.ply_stats.v0.1",
        "path": str(path),
        "format": header["format"],
        "vertex_count": vertex_count,
        "splat_count": vertex_count,
        "non_finite_count": non_finite,
        "finite": non_finite == 0,
        "scale_summary": scale_summary,
        "radius_summary": radius_summary,
    }
    return summary


def sanitize_ply_drop_non_finite(path: Path, out_path: Path | None = None) -> dict[str, Any]:
    path = path.resolve()
    out_path = (out_path or path.with_name(f"{path.stem}.finite.ply")).resolve()
    with path.open("rb") as handle:
        raw_header: list[bytes] = []
        while True:
            raw = handle.readline()
            if raw == b"":
                raise ValueError("PLY header ended before end_header")
            raw_header.append(raw)
            line = raw.decode("ascii").strip()
            if line == "end_header":
                break
        data_offset = handle.tell()

    with path.open("rb") as header_handle:
        header, _ = _parse_header(header_handle)
    vertex_element = next((element for element in header["elements"] if element["name"] == "vertex"), None)
    if vertex_element is None:
        raise ValueError("PLY has no vertex element")
    vertex_count = int(vertex_element["count"])
    vertex_properties = vertex_element["properties"]
    extra_elements = [element for element in header["elements"] if element["name"] != "vertex" and int(element["count"]) > 0]
    if extra_elements:
        names = ", ".join(element["name"] for element in extra_elements)
        raise ValueError(f"cannot sanitize PLY files with non-vertex elements: {names}")

    kept_rows: list[bytes | str] = []
    dropped: list[dict[str, Any]] = []
    if header["format"] == "ascii":
        text = path.read_text(encoding="ascii")
        body = text[text.index("end_header") + len("end_header"):].strip().splitlines()
        for row_index, line in enumerate(body[:vertex_count]):
            parts = line.split()
            if len(parts) < len(vertex_properties):
                raise ValueError(f"vertex row {row_index + 1} has too few columns")
            bad_properties = []
            for index, prop in enumerate(vertex_properties):
                if prop["kind"] != "scalar" or prop["type"] not in PLY_TYPES:
                    continue
                value = float(parts[index])
                if not math.isfinite(value):
                    bad_properties.append(prop["name"])
            if bad_properties:
                dropped.append({"row": row_index, "properties": bad_properties})
            else:
                kept_rows.append(line)
    else:
        endian = "<" if header["format"] == "binary_little_endian" else ">"
        row_format_parts = []
        for prop in vertex_properties:
            if prop["kind"] == "list":
                raise ValueError("cannot sanitize PLY files with list vertex properties")
            if prop["type"] not in PLY_TYPES:
                raise ValueError(f"unsupported PLY property type: {prop['type']}")
            row_format_parts.append(PLY_TYPES[prop["type"]][0])
        row_format = endian + "".join(row_format_parts)
        row_size = struct.calcsize(row_format)
        with path.open("rb") as handle:
            handle.seek(data_offset)
            for row_index in range(vertex_count):
                raw = handle.read(row_size)
                if len(raw) != row_size:
                    raise ValueError("PLY binary data ended early")
                values = struct.unpack(row_format, raw)
                bad_properties = [
                    prop["name"]
                    for prop, value in zip(vertex_properties, values)
                    if isinstance(value, float) and not math.isfinite(value)
                ]
                if bad_properties:
                    dropped.append({"row": row_index, "properties": bad_properties})
                else:
                    kept_rows.append(raw)

    out_header = []
    for raw in raw_header:
        line = raw.decode("ascii")
        if line.startswith("element vertex "):
            out_header.append(f"element vertex {len(kept_rows)}\n".encode("ascii"))
        else:
            out_header.append(raw)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if header["format"] == "ascii":
        with out_path.open("w", encoding="ascii") as handle:
            for raw in out_header:
                handle.write(raw.decode("ascii"))
            for row in kept_rows:
                handle.write(str(row) + "\n")
    else:
        with out_path.open("wb") as handle:
            for raw in out_header:
                handle.write(raw)
            for row in kept_rows:
                handle.write(row)  # type: ignore[arg-type]

    output_stats = inspect_ply(out_path)
    report = {
        "schema": "capture_splat.ply_sanitize_report.v0.1",
        "source": str(path),
        "output": str(out_path),
        "method": "drop_vertices_with_non_finite_numeric_properties",
        "source_vertex_count": vertex_count,
        "output_vertex_count": len(kept_rows),
        "dropped_vertex_count": len(dropped),
        "dropped_vertices": dropped[:100],
        "output_ply_stats": output_stats,
    }
    write_json_strict(out_path.with_suffix(out_path.suffix + ".sanitize_report.json"), report)
    return report


def write_ply_stats(path: Path, out_dir: Path) -> dict[str, Any]:
    summary = inspect_ply(path)
    write_json_strict(out_dir / "capture_splat_ply_stats.json", summary)
    return summary
