import hashlib
from pathlib import Path
import struct

import pytest

from capture_splat.json_utils import load_json_strict, write_json_strict
from capture_splat.spz_export import export_spz


def write_gaussian_ply(path: Path) -> None:
    path.write_text(
        "\n".join([
            "ply",
            "format ascii 1.0",
            "element vertex 3",
            "property float x",
            "property float y",
            "property float z",
            "property float f_dc_0",
            "property float f_dc_1",
            "property float f_dc_2",
            "property float opacity",
            "property float scale_0",
            "property float scale_1",
            "property float scale_2",
            "property float rot_0",
            "property float rot_1",
            "property float rot_2",
            "property float rot_3",
            "end_header",
            "0 0 0 0.1 0.2 0.3 2 -2 -2 -2 1 0 0 0",
            "1 0 0 0.2 0.3 0.4 2 -2 -2 -2 1 0 0 0",
            "0 1 1 0.3 0.4 0.5 2 -2 -2 -2 1 0 0 0",
        ]) + "\n",
        encoding="ascii",
    )


def spz_bytes(count: int = 3) -> bytes:
    header = struct.pack(
        "<IIIBBBBI12s",
        0x5053474E,
        4,
        count,
        0,
        12,
        0,
        6,
        32,
        b"\0" * 12,
    )
    return header + b"\0" * (6 * 16)


def write_fake_converter(path: Path, fail: bool = False) -> None:
    path.write_text(
        "from pathlib import Path\n"
        "import shutil, struct, sys\n"
        f"FAIL = {fail!r}\n"
        "if FAIL:\n"
        "    raise SystemExit(7)\n"
        "source, target = Path(sys.argv[-2]), Path(sys.argv[-1])\n"
        "if target.suffix == '.spz':\n"
        "    text = source.read_text(encoding='ascii')\n"
        "    count = int(next(line.split()[2] for line in text.splitlines() if line.startswith('element vertex ')))\n"
        "    header = struct.pack('<IIIBBBBI12s', 0x5053474E, 4, count, 0, 12, 0, 6, 32, b'\\0' * 12)\n"
        "    target.write_bytes(header + b'\\0' * (6 * 16))\n"
        "    shutil.copy2(source, Path(str(target) + '.source.ply'))\n"
        "else:\n"
        "    shutil.copy2(Path(str(source) + '.source.ply'), target)\n",
        encoding="utf-8",
    )


def test_spz_export_roundtrip_holds_without_viewer_evidence(tmp_path: Path) -> None:
    source = tmp_path / "splat.ply"
    write_gaussian_ply(source)
    converter = tmp_path / "fake_converter.py"
    write_fake_converter(converter)
    output = tmp_path / "scene.spz"

    summary = export_spz(source, output, converter=converter)

    assert summary["decision"] == "hold"
    assert summary["spz"]["header"]["version"] == 4
    assert summary["spz"]["header"]["splat_count"] == 3
    assert summary["roundtrip"]["position_p95_scene_diagonal_fraction"] == 0
    assert summary["roundtrip"]["color_mean_absolute_error"] == 0
    assert summary["viewer_evidence"]["reason"] == "viewer_evidence_not_supplied"
    assert load_json_strict(tmp_path / "scene.spz.export_report.json")["decision"] == "hold"


def test_spz_export_promotes_checksum_bound_viewer_checks(tmp_path: Path) -> None:
    source = tmp_path / "splat.ply"
    write_gaussian_ply(source)
    converter = tmp_path / "fake_converter.py"
    write_fake_converter(converter)
    output = tmp_path / "scene.spz"
    checksum = f"sha256:{hashlib.sha256(spz_bytes()).hexdigest()}"
    evidence = tmp_path / "viewer.json"
    write_json_strict(evidence, {
        "schema": "capture_splat.spz_viewer_evidence.v0.1",
        "spz_checksum": checksum,
        "checks": {
            "viewer_load": True,
            "orientation": True,
            "color": True,
            "source_camera_alignment": True,
        },
    })

    summary = export_spz(source, output, converter=converter, viewer_evidence=evidence)

    assert summary["decision"] == "promote"
    assert summary["viewer_evidence"]["accepted"] is True
    assert summary["authority"]["quality_claim"] is False


def test_spz_export_rejects_non_gaussian_ply(tmp_path: Path) -> None:
    source = tmp_path / "points.ply"
    source.write_text(
        "ply\nformat ascii 1.0\nelement vertex 1\n"
        "property float x\nproperty float y\nproperty float z\nend_header\n0 0 0\n",
        encoding="ascii",
    )
    converter = tmp_path / "fake_converter.py"
    write_fake_converter(converter)

    with pytest.raises(ValueError, match="supported Gaussian PLY"):
        export_spz(source, tmp_path / "scene.spz", converter=converter)


def test_spz_export_writes_reject_report_when_converter_fails(tmp_path: Path) -> None:
    source = tmp_path / "splat.ply"
    write_gaussian_ply(source)
    converter = tmp_path / "fake_converter.py"
    write_fake_converter(converter, fail=True)
    output = tmp_path / "scene.spz"

    with pytest.raises(RuntimeError, match="exit code 7"):
        export_spz(source, output, converter=converter)

    report = load_json_strict(tmp_path / "scene.spz.export_report.json")
    assert report["decision"] == "reject"
    assert report["processes"][0]["returncode"] == 7
