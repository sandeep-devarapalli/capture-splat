from pathlib import Path

import numpy as np
from PIL import Image
import pytest

from capture_splat.equirectangular_import import import_equirectangular
from capture_splat.equirectangular_sfm import (
    COLMAP_FROM_PROJECTION,
    _quaternion_wxyz,
    run_equirectangular_rig_sfm,
)
from capture_splat.json_utils import load_json_strict, write_json_strict


def write_panorama(path: Path, shift: int = 0) -> None:
    pixels = np.zeros((64, 128, 3), dtype=np.uint8)
    pixels[:, (16 + shift):(48 + shift)] = [220, 40, 30]
    pixels[:, (72 + shift):(104 + shift)] = [20, 180, 90]
    Image.fromarray(pixels).save(path)


def make_package(tmp_path: Path, panorama_count: int = 2) -> Path:
    source = tmp_path / "panoramas"
    source.mkdir()
    for index in range(panorama_count):
        write_panorama(source / f"{index:06d}.png", shift=index * 2)
    package = tmp_path / "imported"
    import_equirectangular(source, package, size=32, fov_degrees=100)
    return package


def enable_colmap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("capture_splat.equirectangular_sfm.find_binary", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("capture_splat.equirectangular_sfm.colmap_has_cuda", lambda: True)
    monkeypatch.setattr(
        "capture_splat.equirectangular_sfm.colmap_capabilities",
        lambda: {"global_mapper": True, "rig_configurator": True},
    )


def write_fake_rig_model(out_dir: Path, panorama_count: int, model_name: str = "0") -> None:
    sparse = out_dir / "sparse" / model_name
    sparse.mkdir(parents=True)
    config = load_json_strict(out_dir / "metadata/colmap_rig_config.json")[0]["cameras"]
    camera_lines = ["# cameras"]
    rig_tokens = ["1", str(len(config)), "CAMERA", "1"]
    for camera_id, camera in enumerate(config, start=1):
        camera_lines.append(f"{camera_id} PINHOLE 32 32 16 16 16 16")
        if camera_id > 1:
            pose = [
                *camera["cam_from_rig_rotation"],
                *camera["cam_from_rig_translation"],
            ]
            rig_tokens.extend(["CAMERA", str(camera_id), "1", *(str(value) for value in pose)])
    image_lines = ["# images"]
    frame_lines = ["# frames"]
    image_id = 1
    for panorama_id in range(1, panorama_count + 1):
        frame_data = []
        for camera_id in range(1, len(config) + 1):
            image_lines.extend([
                f"{image_id} 1 0 0 0 0 0 0 {camera_id} "
                f"pano_camera{camera_id - 1:02d}/p{panorama_id:06d}.png",
                "",
            ])
            frame_data.extend(["CAMERA", str(camera_id), str(image_id)])
            image_id += 1
        frame_lines.append(
            " ".join([
                str(panorama_id), "1", "1", "0", "0", "0", "0", "0", "0",
                str(len(config)), *frame_data,
            ])
        )
    (sparse / "cameras.txt").write_text("\n".join(camera_lines) + "\n", encoding="utf-8")
    (sparse / "images.txt").write_text("\n".join(image_lines) + "\n", encoding="utf-8")
    (sparse / "rigs.txt").write_text(" ".join(rig_tokens) + "\n", encoding="utf-8")
    (sparse / "frames.txt").write_text("\n".join(frame_lines) + "\n", encoding="utf-8")
    (sparse / "points3D.txt").write_text(
        "1 0 0 0 100 120 140 0.5 1 0 2 0\n",
        encoding="utf-8",
    )


def test_sfm_360_rig_dry_run_materializes_fixed_rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    package = make_package(tmp_path)
    enable_colmap(monkeypatch)

    summary = run_equirectangular_rig_sfm(package, tmp_path / "out", dry_run=True)

    assert summary["decision"] == "dry_run"
    assert summary["panorama_count"] == 2
    assert summary["virtual_camera_count"] == 14
    assert summary["projection_count"] == 28
    assert len(list((tmp_path / "out/images").glob("*/*.png"))) == 28
    assert len(list((tmp_path / "out/masks").glob("*/*.png"))) == 28
    config = load_json_strict(tmp_path / "out/metadata/colmap_rig_config.json")
    assert config[0]["cameras"][0] == {
        "image_prefix": "pano_camera00/",
        "ref_sensor": True,
    }
    assert config[0]["cameras"][1]["cam_from_rig_translation"] == [0.0, 0.0, 0.0]
    assert len(config[0]["cameras"][1]["cam_from_rig_rotation"]) == 4
    commands = summary["commands"]
    assert [command[1] for command in commands] == [
        "feature_extractor", "rig_configurator", "sequential_matcher", "global_mapper"
    ]
    assert "--ImageReader.single_camera_per_folder" in commands[0]
    assert commands[0][commands[0].index("--FeatureExtraction.use_gpu") + 1] == "1"
    assert "--FeatureMatching.rig_verification" in commands[2]
    assert "--FeatureMatching.skip_image_pairs_in_same_frame" in commands[2]
    assert commands[2][commands[2].index("--FeatureMatching.use_gpu") + 1] == "1"
    assert commands[3][commands[3].index("--GlobalMapper.refine_sensor_from_rig") + 1] == "0"
    assert summary["authority"]["recovered_world_poses"] is False
    assert summary["authority"]["rig_extrinsics_requested"] is True
    assert summary["authority"]["rig_config_applied"] is False
    assert summary["authority"]["output_rig_validated"] is False


def test_colmap_rig_rotation_conjugates_projection_y_up() -> None:
    angle = np.deg2rad(45)
    world_from_upper = np.asarray([
        [1, 0, 0],
        [0, np.cos(angle), np.sin(angle)],
        [0, -np.sin(angle), np.cos(angle)],
    ])
    expected = COLMAP_FROM_PROJECTION @ world_from_upper.T @ COLMAP_FROM_PROJECTION

    quaternion = _quaternion_wxyz(expected)

    assert quaternion[0] == pytest.approx(np.cos(angle / 2))
    assert quaternion[1] == pytest.approx(-np.sin(angle / 2))
    assert quaternion[2:] == pytest.approx([0, 0])


def test_sfm_360_rig_rejects_changed_projection(tmp_path: Path) -> None:
    package = make_package(tmp_path, panorama_count=1)
    first = next((package / "images").glob("*.png"))
    first.write_bytes(b"changed")

    with pytest.raises(ValueError, match="size mismatch|checksum mismatch"):
        run_equirectangular_rig_sfm(package, tmp_path / "out", dry_run=True)


def test_sfm_360_rig_cpu_override_disables_all_colmap_gpu_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = make_package(tmp_path, panorama_count=1)
    monkeypatch.setattr("capture_splat.equirectangular_sfm.find_binary", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("capture_splat.equirectangular_sfm.colmap_has_cuda", lambda: False)
    monkeypatch.setattr(
        "capture_splat.equirectangular_sfm.colmap_capabilities",
        lambda: {"global_mapper": True, "rig_configurator": True},
    )

    summary = run_equirectangular_rig_sfm(
        package,
        tmp_path / "out",
        allow_cpu_matching=True,
        dry_run=True,
    )

    assert summary["commands"][0][summary["commands"][0].index("--FeatureExtraction.use_gpu") + 1] == "0"
    assert summary["commands"][2][summary["commands"][2].index("--FeatureMatching.use_gpu") + 1] == "0"
    assert summary["commands"][3][summary["commands"][3].index("--GlobalMapper.gp_use_gpu") + 1] == "0"
    assert summary["commands"][3][summary["commands"][3].index("--GlobalMapper.ba_ceres_use_gpu") + 1] == "0"


def test_sfm_360_rig_rejects_non_rotation(tmp_path: Path) -> None:
    package = make_package(tmp_path, panorama_count=1)
    rig_path = package / "metadata/equirectangular_rig.json"
    rig = load_json_strict(rig_path)
    rig["virtual_views"][0]["rotation_equirect_world_from_camera"][0][0] = 2.0
    write_json_strict(rig_path, rig)

    with pytest.raises(ValueError, match="orthonormal"):
        run_equirectangular_rig_sfm(package, tmp_path / "out", dry_run=True)


def test_sfm_360_rig_rejects_path_escape_and_invalid_intrinsics(tmp_path: Path) -> None:
    package = make_package(tmp_path, panorama_count=1)
    rig_path = package / "metadata/equirectangular_rig.json"
    rig = load_json_strict(rig_path)
    rig["virtual_views"][0]["image"] = "../outside.png"
    write_json_strict(rig_path, rig)
    with pytest.raises(ValueError, match="escapes package"):
        run_equirectangular_rig_sfm(package, tmp_path / "out_escape", dry_run=True)

    rig = load_json_strict(rig_path)
    rig["virtual_views"][0]["image"] = "images/p000001_v01.png"
    rig["intrinsics"]["fl_x"] = 0
    write_json_strict(rig_path, rig)
    with pytest.raises(ValueError, match="focal lengths must be positive"):
        run_equirectangular_rig_sfm(package, tmp_path / "out_intrinsics", dry_run=True)


def test_sfm_360_rig_records_panorama_level_registration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = make_package(tmp_path)
    enable_colmap(monkeypatch)

    class Completed:
        returncode = 0

    def fake_run(command: list[str], **_: object) -> Completed:
        if command[1] == "global_mapper":
            write_fake_rig_model(tmp_path / "out", panorama_count=2)
        return Completed()

    monkeypatch.setattr("capture_splat.equirectangular_sfm.subprocess.run", fake_run)

    summary = run_equirectangular_rig_sfm(package, tmp_path / "out")

    assert summary["decision"] == "promote"
    assert summary["registered_panorama_count"] == 2
    assert summary["registered_panorama_ratio"] == 1.0
    assert summary["output_rig"]["valid"] is True
    assert summary["output_rig"]["sensor_count"] == 14
    assert summary["authority"]["rig_config_applied"] is True
    assert summary["authority"]["output_rig_validated"] is True
    assert summary["authority"]["recovered_world_poses"] is True
    assert summary["authority"]["metric_geometry"] is False


def test_sfm_360_rig_prefers_valid_rig_over_denser_fragment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = make_package(tmp_path)
    enable_colmap(monkeypatch)

    class Completed:
        returncode = 0

    def fake_run(command: list[str], **_: object) -> Completed:
        if command[1] == "global_mapper":
            invalid = tmp_path / "out/sparse/0"
            invalid.mkdir(parents=True)
            (invalid / "images.txt").write_text(
                "1 1 0 0 0 0 0 0 1 pano_camera00/p000001.png\n\n",
                encoding="utf-8",
            )
            (invalid / "points3D.txt").write_text("x" * 5000, encoding="utf-8")
            write_fake_rig_model(tmp_path / "out", panorama_count=2, model_name="1")
        return Completed()

    monkeypatch.setattr("capture_splat.equirectangular_sfm.subprocess.run", fake_run)

    summary = run_equirectangular_rig_sfm(package, tmp_path / "out")

    assert summary["decision"] == "promote"
    assert summary["output_rig"]["valid"] is True
    assert (tmp_path / "out/sparse/old_0").exists()
    assert (tmp_path / "out/sparse/0/rigs.txt").exists()
