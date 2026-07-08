from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

from .colmap_support_repair import build_colmap_support_repair
from .json_utils import load_json_strict, write_json_strict
from .weak_frames_report import _frame_key


def _read_image_list(path: Path) -> list[str]:
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _write_lines(path: Path, rows: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")


def _database_image_ids(database: Path) -> dict[str, int]:
    with sqlite3.connect(database) as connection:
        rows = connection.execute("select image_id, name from images").fetchall()
    return {str(name): int(image_id) for image_id, name in rows}


def _parse_image_rows(path: Path) -> tuple[list[str], list[tuple[list[str], str]]]:
    comments: list[str] = []
    data: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("#"):
            comments.append(line)
        elif line.strip():
            data.append(line.rstrip("\n"))
    rows: list[tuple[list[str], str]] = []
    for index in range(0, len(data), 2):
        pose = data[index].split()
        points = data[index + 1] if index + 1 < len(data) else ""
        if len(pose) >= 10:
            rows.append((pose, points))
    return comments, rows


def _camera_ids(path: Path) -> list[int]:
    ids = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        ids.append(int(line.split()[0]))
    return ids


def _model_image_names(package: Path, sparse_dir_name: str) -> list[str]:
    _, rows = _parse_image_rows(package / sparse_dir_name / "images.txt")
    return [pose[9] for pose, _points in rows]


def _first_camera_options(path: Path) -> tuple[str, str]:
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 5:
            raise ValueError(f"invalid COLMAP camera row: {line}")
        return parts[1], ",".join(parts[4:])
    raise ValueError(f"no camera rows found in {path}")


def _rewrite_points3d(path: Path, out_path: Path, image_id_map: dict[int, int]) -> None:
    lines = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            lines.append(line)
            continue
        parts = line.split()
        rewritten = parts[:8]
        track = parts[8:]
        for index in range(0, len(track), 2):
            if index + 1 >= len(track):
                break
            new_id = image_id_map.get(int(track[index]))
            if new_id is not None:
                rewritten.extend([str(new_id), track[index + 1]])
        lines.append(" ".join(rewritten))
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_database_aligned_sparse_input(
    package: Path,
    out_dir: Path,
    database_image_ids: dict[str, int],
    image_names: list[str] | None = None,
    sparse_dir_name: str = "sparse/0",
    preserve_existing_points: bool = False,
) -> dict[str, Any]:
    sparse = package / sparse_dir_name
    cameras = sparse / "cameras.txt"
    images = sparse / "images.txt"
    if not cameras.exists() or not images.exists():
        raise FileNotFoundError(f"COLMAP sparse text model missing under {sparse}")

    selected = set(image_names) if image_names else None
    out_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(cameras, out_dir / "cameras.txt")

    comments, rows = _parse_image_rows(images)
    written = []
    missing = []
    image_id_map: dict[int, int] = {}
    image_lines = list(comments)
    frame_lines = [
        "# Frame list with one line of data per frame:",
        "#   FRAME_ID, RIG_ID, RIG_FROM_WORLD[QW, QX, QY, QZ, TX, TY, TZ], NUM_DATA_IDS, DATA_IDS[] as (SENSOR_TYPE, SENSOR_ID, DATA_ID)",
    ]
    for pose, points in rows:
        name = pose[9]
        if selected is not None and name not in selected:
            continue
        image_id = database_image_ids.get(name)
        if image_id is None:
            missing.append(name)
            continue
        camera_id = int(pose[8])
        image_id_map[int(pose[0])] = image_id
        rewritten = [str(image_id), *pose[1:]]
        image_lines.extend([" ".join(rewritten), points if preserve_existing_points else ""])
        frame_lines.append(
            " ".join([
                str(image_id),
                "1",
                *pose[1:8],
                "1",
                "CAMERA",
                str(camera_id),
                str(image_id),
            ])
        )
        written.append(name)

    rig_lines = [
        "# Rig calib list with one line of data per calib:",
        "#   RIG_ID, NUM_SENSORS, REF_SENSOR_TYPE, REF_SENSOR_ID, SENSORS[] as (SENSOR_TYPE, SENSOR_ID, HAS_POSE, [QW, QX, QY, QZ, TX, TY, TZ])",
    ]
    camera_ids = _camera_ids(cameras)
    if camera_ids:
        ref = camera_ids[0]
        sensors: list[str] = []
        for camera_id in camera_ids[1:]:
            sensors.extend(["CAMERA", str(camera_id), "0"])
        rig_lines.append(" ".join(["1", str(len(camera_ids)), "CAMERA", str(ref), *sensors]))

    (out_dir / "images.txt").write_text("\n".join(image_lines) + "\n", encoding="utf-8")
    if preserve_existing_points:
        _rewrite_points3d(sparse / "points3D.txt", out_dir / "points3D.txt", image_id_map)
    else:
        (out_dir / "points3D.txt").write_text(
            "# 3D point list with one line of data per point:\n# Number of points: 0, mean track length: 0\n",
            encoding="utf-8",
        )
    (out_dir / "rigs.txt").write_text("\n".join(rig_lines) + "\n", encoding="utf-8")
    (out_dir / "frames.txt").write_text("\n".join(frame_lines) + "\n", encoding="utf-8")
    return {
        "sparse_input": str(out_dir),
        "image_count": len(written),
        "missing_from_database": missing,
        "has_rigs_txt": True,
        "has_frames_txt": True,
        "preserve_existing_points": preserve_existing_points,
    }


def _image_name_by_frame(package: Path, sparse_dir_name: str) -> dict[str, str]:
    return {_frame_key(name): name for name in _model_image_names(package, sparse_dir_name)}


def _parse_bridge_ranges(value: str | None, names_by_frame: dict[str, str]) -> list[list[str]]:
    if not value:
        return []
    ranges: list[list[str]] = []
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        if "-" in item:
            left, right = item.split("-", 1)
            start = int(_frame_key(left))
            end = int(_frame_key(right))
            lo, hi = sorted((start, end))
            frames = [f"{frame:06d}" for frame in range(lo, hi + 1) if f"{frame:06d}" in names_by_frame]
        else:
            key = _frame_key(item)
            frames = [key] if key in names_by_frame else []
        if frames:
            ranges.append(frames)
    return ranges


def _prepare_repair_inputs(
    package: Path,
    out_dir: Path,
    base_image_list: Path,
    base_pairs: Path,
    sparse_dir_name: str,
    include_all_registered_images: bool,
    bridge_ranges: str | None,
    bridge_window: int,
) -> dict[str, Any]:
    if bridge_window < 1:
        raise ValueError("bridge_window must be positive")
    names_by_frame = _image_name_by_frame(package, sparse_dir_name)
    image_names = set(_read_image_list(base_image_list))
    if include_all_registered_images:
        image_names.update(_model_image_names(package, sparse_dir_name))

    pair_rows = {tuple(line.split()[:2]) for line in base_pairs.read_text(encoding="utf-8").splitlines() if line.strip()}
    added_pairs: set[tuple[str, str]] = set()
    for frames in _parse_bridge_ranges(bridge_ranges, names_by_frame):
        for left_index, left in enumerate(frames):
            for right in frames[left_index + 1:left_index + 1 + bridge_window]:
                pair = tuple(sorted((names_by_frame[left], names_by_frame[right])))
                pair_rows.add(pair)
                added_pairs.add(pair)
                image_names.update(pair)

    out_image_list = out_dir / "repair_image_list.broader.txt"
    out_pairs = out_dir / "repair_pairs.broader.txt"
    _write_lines(out_image_list, sorted(image_names))
    _write_lines(out_pairs, [f"{left} {right}" for left, right in sorted(pair_rows)])
    return {
        "image_list": out_image_list,
        "pairs": out_pairs,
        "include_all_registered_images": include_all_registered_images,
        "bridge_ranges": bridge_ranges,
        "bridge_window": bridge_window,
        "selected_image_count": len(image_names),
        "pair_count": len(pair_rows),
        "added_bridge_pair_count": len(added_pairs),
    }


def _run(command: list[str], log_path: Path) -> dict[str, Any]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    log_path.write_text(completed.stdout or "", encoding="utf-8")
    return {"command": command, "returncode": completed.returncode, "log": str(log_path)}


def _command_plan(
    colmap: str,
    package: Path,
    workspace: Path,
    image_list: Path,
    pairs: Path,
    database: Path,
    input_sparse: Path,
    output_sparse: Path,
    image_dir_name: str,
    sparse_dir_name: str,
    max_num_features: int,
    use_gpu: bool,
    preserve_existing_points: bool,
) -> list[list[str]]:
    gpu = "1" if use_gpu else "0"
    camera_model, camera_params = _first_camera_options(package / sparse_dir_name / "cameras.txt")
    return [
        [
            colmap,
            "feature_extractor",
            "--database_path",
            str(database),
            "--image_path",
            str(package / image_dir_name),
            "--image_list_path",
            str(image_list),
            "--ImageReader.single_camera",
            "1",
            "--ImageReader.camera_model",
            camera_model,
            "--ImageReader.camera_params",
            camera_params,
            "--SiftExtraction.use_gpu",
            gpu,
            "--SiftExtraction.max_num_features",
            str(max_num_features),
        ],
        [
            colmap,
            "matches_importer",
            "--database_path",
            str(database),
            "--match_list_path",
            str(pairs),
            "--match_type",
            "pairs",
            "--SiftMatching.use_gpu",
            gpu,
            "--SiftMatching.guided_matching",
            "1",
        ],
        [
            colmap,
            "point_triangulator",
            "--database_path",
            str(database),
            "--image_path",
            str(package / image_dir_name),
            "--input_path",
            str(input_sparse),
            "--output_path",
            str(output_sparse),
            "--clear_points",
            "0" if preserve_existing_points else "1",
            "--refine_intrinsics",
            "0",
            "--Mapper.ba_refine_focal_length",
            "0",
            "--Mapper.ba_refine_principal_point",
            "0",
            "--Mapper.ba_refine_extra_params",
            "0",
            "--Mapper.extract_colors",
            "1",
        ],
    ]


def run_colmap_focused_repair(
    package: Path,
    out_dir: Path,
    weak_report: Path | None = None,
    repair_manifest: Path | None = None,
    image_dir_name: str = "images",
    sparse_dir_name: str = "sparse/0",
    neighbor_radius: int = 4,
    max_anchors_per_target: int = 8,
    max_num_features: int = 16384,
    use_gpu: bool = False,
    include_all_registered_images: bool = False,
    bridge_ranges: str | None = None,
    bridge_window: int = 6,
    preserve_existing_points: bool = False,
    dry_run: bool = False,
    colmap_binary: str = "colmap",
) -> dict[str, Any]:
    package = package.resolve()
    out_dir = out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    if repair_manifest is None:
        if weak_report is None:
            raise ValueError("either weak_report or repair_manifest is required")
        manifest = build_colmap_support_repair(
            weak_report,
            package,
            out_dir / "support_manifest",
            image_dir_name=image_dir_name,
            neighbor_radius=neighbor_radius,
            max_anchors_per_target=max_anchors_per_target,
        )
        repair_manifest_path = out_dir / "support_manifest" / "capture_splat_colmap_support_repair_manifest.json"
    else:
        repair_manifest_path = repair_manifest.resolve()
        manifest = load_json_strict(repair_manifest_path)

    outputs = manifest.get("outputs") if isinstance(manifest, dict) else {}
    image_list = Path(str(outputs.get("repair_image_list") or ""))
    pairs = Path(str(outputs.get("repair_pairs") or ""))
    if not image_list.exists() or not pairs.exists():
        raise FileNotFoundError("repair manifest must reference repair_image_list and repair_pairs")
    repair_inputs = _prepare_repair_inputs(
        package,
        out_dir,
        image_list,
        pairs,
        sparse_dir_name,
        include_all_registered_images,
        bridge_ranges,
        bridge_window,
    )
    image_list = repair_inputs["image_list"]
    pairs = repair_inputs["pairs"]

    workspace = out_dir / "colmap_workspace"
    database = workspace / "database.db"
    input_sparse = workspace / "sparse_input"
    output_sparse = workspace / "sparse_repaired"
    output_sparse_text = workspace / "sparse_repaired_text"
    colmap = shutil.which(colmap_binary) or colmap_binary
    commands = _command_plan(
        colmap,
        package,
        workspace,
        image_list,
        pairs,
        database,
        input_sparse,
        output_sparse,
        image_dir_name,
        sparse_dir_name,
        max_num_features,
        use_gpu,
        preserve_existing_points,
    )
    summary: dict[str, Any] = {
        "schema": "capture_splat.colmap_focused_repair.v0.1",
        "decision": "hold",
        "authority": {
            "support_evidence_only": True,
            "colmap_repair_complete": False,
            "training_result": False,
            "quality_claim": False,
        },
        "package": str(package),
        "repair_manifest": str(repair_manifest_path),
        "workspace": str(workspace),
        "dry_run": dry_run,
        "commands": commands,
        "repair_inputs": {
            **repair_inputs,
            "image_list": str(image_list),
            "pairs": str(pairs),
        },
        "preserve_existing_points": preserve_existing_points,
        "outputs": {
            "database": str(database),
            "sparse_input": str(input_sparse),
            "sparse_repaired": str(output_sparse),
            "sparse_repaired_text": str(output_sparse_text),
        },
    }
    if dry_run:
        write_json_strict(out_dir / "capture_splat_colmap_focused_repair_summary.json", summary)
        return summary

    if shutil.which(colmap_binary) is None and not Path(colmap_binary).exists():
        summary["status"] = "blocked"
        summary["blocker"] = f"COLMAP binary not found: {colmap_binary}"
        write_json_strict(out_dir / "capture_splat_colmap_focused_repair_summary.json", summary)
        return summary

    workspace.mkdir(parents=True, exist_ok=True)
    if database.exists():
        database.unlink()
    logs = workspace / "logs"
    step_results = []
    for index, command in enumerate(commands[:2], start=1):
        result = _run(command, logs / f"{index:02d}_{command[1]}.log")
        step_results.append(result)
        if result["returncode"] != 0:
            summary.update({"status": "blocked", "blocker": f"{command[1]} failed", "steps": step_results})
            write_json_strict(out_dir / "capture_splat_colmap_focused_repair_summary.json", summary)
            return summary

    ids = _database_image_ids(database)
    sparse_summary = write_database_aligned_sparse_input(
        package,
        input_sparse,
        ids,
        image_names=_read_image_list(image_list),
        sparse_dir_name=sparse_dir_name,
        preserve_existing_points=preserve_existing_points,
    )
    summary["database_aligned_sparse"] = sparse_summary
    output_sparse.mkdir(parents=True, exist_ok=True)
    result = _run(commands[2], logs / "03_point_triangulator.log")
    step_results.append(result)
    summary["steps"] = step_results
    if result["returncode"] != 0:
        summary.update({"status": "blocked", "blocker": "point_triangulator failed"})
    else:
        converted_images = output_sparse / "images.txt"
        if not converted_images.exists() and (output_sparse / "images.bin").exists():
            output_sparse_text.mkdir(parents=True, exist_ok=True)
            convert_command = [
                colmap,
                "model_converter",
                "--input_path",
                str(output_sparse),
                "--output_path",
                str(output_sparse_text),
                "--output_type",
                "TXT",
            ]
            convert_result = _run(convert_command, logs / "04_model_converter.log")
            step_results.append(convert_result)
            converted_images = output_sparse_text / "images.txt"
        if converted_images.exists():
            summary["status"] = "completed"
            summary["decision"] = "support_repair_ready_for_delta"
            summary["authority"]["colmap_repair_complete"] = True
            summary["repaired_images"] = str(converted_images)
        else:
            summary.update({"status": "blocked", "blocker": "point_triangulator produced no images.txt"})
    if summary.get("status") == "completed":
        summary["status"] = "completed"
        summary["decision"] = "support_repair_ready_for_delta"
        summary["authority"]["colmap_repair_complete"] = True
    write_json_strict(out_dir / "capture_splat_colmap_focused_repair_summary.json", summary)
    return summary


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a focused COLMAP support repair from weak-frame evidence.")
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--weak-report", type=Path)
    parser.add_argument("--repair-manifest", type=Path)
    parser.add_argument("--image-dir", default="images")
    parser.add_argument("--sparse-dir", default="sparse/0")
    parser.add_argument("--neighbor-radius", type=int, default=4)
    parser.add_argument("--max-anchors-per-target", type=int, default=8)
    parser.add_argument("--max-num-features", type=int, default=16384)
    parser.add_argument("--use-gpu", action="store_true")
    parser.add_argument("--include-all-registered-images", action="store_true")
    parser.add_argument("--bridge-ranges")
    parser.add_argument("--bridge-window", type=int, default=6)
    parser.add_argument("--preserve-existing-points", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--colmap-binary", default="colmap")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    summary = run_colmap_focused_repair(
        args.package,
        args.out,
        weak_report=args.weak_report,
        repair_manifest=args.repair_manifest,
        image_dir_name=args.image_dir,
        sparse_dir_name=args.sparse_dir,
        neighbor_radius=args.neighbor_radius,
        max_anchors_per_target=args.max_anchors_per_target,
        max_num_features=args.max_num_features,
        use_gpu=args.use_gpu,
        include_all_registered_images=args.include_all_registered_images,
        bridge_ranges=args.bridge_ranges,
        bridge_window=args.bridge_window,
        preserve_existing_points=args.preserve_existing_points,
        dry_run=args.dry_run,
        colmap_binary=args.colmap_binary,
    )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
