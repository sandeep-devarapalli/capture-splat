from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import struct
import subprocess
import sys
from pathlib import Path
from typing import Any

import numpy as np

from .json_utils import load_json_strict, write_json_strict
from .ply_stats import inspect_ply, load_ply_scalar_samples, ply_vertex_property_names

SUMMARY_SCHEMA = "capture_splat.spz_export_summary.v0.1"
VIEWER_EVIDENCE_SCHEMA = "capture_splat.spz_viewer_evidence.v0.1"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _file_evidence(path: Path) -> dict[str, Any]:
    return {
        "path": str(path.resolve()),
        "size_bytes": path.stat().st_size,
        "checksum": _sha256(path),
    }


def _resolve_converter(converter: Path | None) -> Path:
    if converter is not None:
        resolved = converter.expanduser().resolve()
        if not resolved.is_file():
            raise FileNotFoundError(f"splat-transform converter missing: {resolved}")
        return resolved
    found = shutil.which("splat-transform")
    if found is None:
        raise RuntimeError(
            "splat-transform is required; install the optional external converter "
            "with npm install -g @playcanvas/splat-transform"
        )
    return Path(found).resolve()


def _command(converter: Path, source: Path, target: Path, *, spz_version: int | None = None) -> list[str]:
    command = [sys.executable, str(converter)] if converter.suffix.lower() == ".py" else [str(converter)]
    if spz_version is not None:
        command += ["--spz-version", str(spz_version)]
    return command + [str(source), str(target)]


def _spz_v4_header(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()[:32]
    if len(raw) < 32 or raw[:4] != b"NGSP":
        raise ValueError("SPZ output is not a version-4 NGSP file")
    magic, version, count, sh_degree, fractional_bits, flags, streams, toc_offset = struct.unpack(
        "<IIIBBBBI", raw[:20]
    )
    if magic != 0x5053474E or version != 4:
        raise ValueError(f"unexpected SPZ header: magic={magic:#x} version={version}")
    if not 0 <= sh_degree <= 4 or toc_offset < 32:
        raise ValueError("SPZ header fields are out of range")
    if path.stat().st_size < toc_offset + streams * 16:
        raise ValueError("SPZ output ends before its stream table")
    return {
        "version": version,
        "splat_count": count,
        "sh_degree": sh_degree,
        "fractional_bits": fractional_bits,
        "flags": flags,
        "stream_count": streams,
        "toc_byte_offset": toc_offset,
    }


def _roundtrip_metrics(source: Path, roundtrip: Path, sample_limit: int) -> dict[str, Any]:
    source_stats = inspect_ply(source)
    roundtrip_stats = inspect_ply(roundtrip)
    source_properties = set(ply_vertex_property_names(source))
    roundtrip_properties = set(ply_vertex_property_names(roundtrip))
    color_names = (
        ["f_dc_0", "f_dc_1", "f_dc_2"]
        if {"f_dc_0", "f_dc_1", "f_dc_2"} <= source_properties
        else ["red", "green", "blue"]
    )
    required = ["x", "y", "z", *color_names]
    if not set(required) <= roundtrip_properties:
        raise ValueError("round-trip PLY is missing position or color properties")
    before = load_ply_scalar_samples(source, required, sample_limit)
    after = load_ply_scalar_samples(roundtrip, required, sample_limit)
    sample_count = len(before["x"])
    if any(len(after[name]) != sample_count for name in required):
        raise ValueError("round-trip PLY sample count changed")
    before_xyz = np.column_stack([before[name] for name in ("x", "y", "z")])
    after_xyz = np.column_stack([after[name] for name in ("x", "y", "z")])
    diagonal = float(np.linalg.norm(np.ptp(before_xyz, axis=0)))
    position_errors = np.linalg.norm(after_xyz - before_xyz, axis=1)
    p95_fraction = float(np.percentile(position_errors, 95)) / max(diagonal, 1e-12)
    before_color = np.column_stack([before[name] for name in color_names])
    after_color = np.column_stack([after[name] for name in color_names])
    if color_names[0] == "red":
        before_color /= 255.0
        after_color /= 255.0
    color_mae = float(np.mean(np.abs(after_color - before_color)))
    return {
        "source_ply": source_stats,
        "roundtrip_ply": roundtrip_stats,
        "sample_count": sample_count,
        "position_p95_scene_diagonal_fraction": p95_fraction,
        "color_property_names": color_names,
        "color_mean_absolute_error": color_mae,
    }


def _viewer_evidence(path: Path | None, spz_checksum: str) -> dict[str, Any]:
    if path is None:
        return {"accepted": False, "reason": "viewer_evidence_not_supplied"}
    evidence = load_json_strict(path.resolve())
    checks = evidence.get("checks")
    required = ("viewer_load", "orientation", "color", "source_camera_alignment")
    if evidence.get("schema") != VIEWER_EVIDENCE_SCHEMA or not isinstance(checks, dict):
        return {"accepted": False, "reason": "viewer_evidence_schema_invalid", "path": str(path.resolve())}
    if evidence.get("spz_checksum") != spz_checksum:
        return {"accepted": False, "reason": "viewer_evidence_checksum_mismatch", "path": str(path.resolve())}
    failed = [name for name in required if checks.get(name) is not True]
    return {
        "accepted": not failed,
        "reason": "accepted" if not failed else "viewer_checks_incomplete",
        "path": str(path.resolve()),
        "checks": {name: checks.get(name) is True for name in required},
        "failed_checks": failed,
    }


def export_spz(
    source_ply: Path,
    output_spz: Path,
    *,
    converter: Path | None = None,
    viewer_evidence: Path | None = None,
    sample_limit: int = 50_000,
    max_position_p95_fraction: float = 0.005,
    max_color_mae: float = 0.03,
    dry_run: bool = False,
) -> dict[str, Any]:
    source_ply = source_ply.resolve()
    output_spz = output_spz.resolve()
    if source_ply.suffix.lower() != ".ply" or not source_ply.is_file():
        raise FileNotFoundError(f"Gaussian PLY missing: {source_ply}")
    if output_spz.suffix.lower() != ".spz":
        raise ValueError("SPZ output must use the .spz extension")
    if output_spz.exists():
        raise FileExistsError(f"SPZ output already exists: {output_spz}")
    if sample_limit <= 0 or max_position_p95_fraction < 0 or max_color_mae < 0:
        raise ValueError("round-trip limits must be non-negative and sample-limit positive")
    properties = set(ply_vertex_property_names(source_ply))
    required_gaussian = {"x", "y", "z", "opacity", "scale_0", "scale_1", "scale_2", "rot_0", "rot_1", "rot_2", "rot_3"}
    if not required_gaussian <= properties:
        raise ValueError("input is not a supported Gaussian PLY")
    if not ({"f_dc_0", "f_dc_1", "f_dc_2"} <= properties or {"red", "green", "blue"} <= properties):
        raise ValueError("Gaussian PLY has no supported base-color properties")
    source_stats = inspect_ply(source_ply)
    if not source_stats["finite"]:
        raise ValueError("Gaussian PLY contains non-finite numeric properties")
    converter_path = _resolve_converter(converter)
    roundtrip = output_spz.with_suffix(".roundtrip.ply")
    commands = [
        _command(converter_path, source_ply, output_spz, spz_version=4),
        _command(converter_path, output_spz, roundtrip),
    ]
    summary: dict[str, Any] = {
        "schema": SUMMARY_SCHEMA,
        "decision": "dry_run" if dry_run else "hold",
        "source": _file_evidence(source_ply),
        "output": str(output_spz),
        "converter": str(converter_path),
        "commands": commands,
        "dry_run": dry_run,
        "thresholds": {
            "sample_limit": sample_limit,
            "max_position_p95_scene_diagonal_fraction": max_position_p95_fraction,
            "max_color_mean_absolute_error": max_color_mae,
        },
        "authority": {
            "distribution_conversion_only": True,
            "quality_claim": False,
            "metric_authority": False,
            "collision_authority": False,
        },
    }
    report_path = output_spz.with_suffix(".spz.export_report.json")
    if dry_run:
        output_spz.parent.mkdir(parents=True, exist_ok=True)
        write_json_strict(report_path, summary)
        return summary
    output_spz.parent.mkdir(parents=True, exist_ok=True)
    process_records = []
    try:
        for command in commands:
            completed = subprocess.run(command, capture_output=True, text=True)
            process_records.append({
                "command": command,
                "returncode": completed.returncode,
                "stdout": completed.stdout,
                "stderr": completed.stderr,
            })
            if completed.returncode != 0:
                raise RuntimeError(f"splat-transform failed with exit code {completed.returncode}")
        header = _spz_v4_header(output_spz)
        metrics = _roundtrip_metrics(source_ply, roundtrip, sample_limit)
        failures = []
        if header["splat_count"] != source_stats["splat_count"]:
            failures.append("spz_splat_count_mismatch")
        if metrics["roundtrip_ply"]["splat_count"] != source_stats["splat_count"]:
            failures.append("roundtrip_splat_count_mismatch")
        if not metrics["roundtrip_ply"]["finite"]:
            failures.append("roundtrip_non_finite")
        if metrics["position_p95_scene_diagonal_fraction"] > max_position_p95_fraction:
            failures.append("roundtrip_position_regression")
        if metrics["color_mean_absolute_error"] > max_color_mae:
            failures.append("roundtrip_color_regression")
        spz_evidence = _file_evidence(output_spz)
        viewer = _viewer_evidence(viewer_evidence, spz_evidence["checksum"])
        summary.update({
            "processes": process_records,
            "spz": {**spz_evidence, "header": header},
            "roundtrip": {**_file_evidence(roundtrip), **metrics},
            "viewer_evidence": viewer,
            "failures": failures,
            "decision": "reject" if failures else ("promote" if viewer["accepted"] else "hold"),
            "warnings": [] if viewer["accepted"] else [viewer["reason"]],
        })
    except Exception as error:
        summary.update({
            "processes": process_records,
            "decision": "reject",
            "error": str(error),
        })
        write_json_strict(report_path, summary)
        raise
    write_json_strict(report_path, summary)
    return summary


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Convert a finite Gaussian PLY to SPZ and verify a PLY round trip.")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--converter", type=Path)
    parser.add_argument("--viewer-evidence", type=Path)
    parser.add_argument("--sample-limit", type=int, default=50_000)
    parser.add_argument("--max-position-p95-fraction", type=float, default=0.005)
    parser.add_argument("--max-color-mae", type=float, default=0.03)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    summary = export_spz(
        args.input,
        args.out,
        converter=args.converter,
        viewer_evidence=args.viewer_evidence,
        sample_limit=args.sample_limit,
        max_position_p95_fraction=args.max_position_p95_fraction,
        max_color_mae=args.max_color_mae,
        dry_run=args.dry_run,
    )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
