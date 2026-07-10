import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

from PIL import Image

from capture_splat.hloc_runner import run_hloc_frontend


def test_hloc_frontend_uses_expected_configs_and_single_camera(tmp_path: Path, monkeypatch) -> None:
    images = tmp_path / "images"
    images.mkdir()
    Image.new("RGB", (8, 6), (1, 2, 3)).save(images / "000001.jpg")
    calls: list[tuple] = []

    def extract_main(conf, image_dir, output_dir):
        calls.append(("extract", conf["name"]))
        path = output_dir / f"{conf['name']}.h5"
        path.touch()
        return path

    extract_features = SimpleNamespace(
        confs={
            "eigenplaces": {"name": "eigenplaces", "output": "global"},
            "aliked-n16": {"name": "aliked-n16", "output": "aliked"},
        },
        main=extract_main,
    )
    match_features = SimpleNamespace(
        confs={"aliked+lightglue": {"name": "lightglue"}},
        main=lambda conf, pairs, feature_output, output_dir: calls.append(
            ("match", conf["name"], feature_output)
        ) or (output_dir / "matches.h5"),
    )
    pairs_from_retrieval = SimpleNamespace(
        main=lambda retrieval, pairs, num_matched: calls.append(("pairs", num_matched)) or pairs.touch()
    )
    reconstruction = SimpleNamespace(
        create_empty_db=lambda database: calls.append(("create_db", database.name)) or database.touch(),
        get_image_ids=lambda database: {"000001.jpg": 1},
        import_features=lambda image_ids, database, features: calls.append(("import_features", features.name)),
        import_matches=lambda *args: calls.append(("import_matches", Path(args[2]).name)),
    )
    hloc = ModuleType("hloc")
    hloc.extract_features = extract_features
    hloc.match_features = match_features
    hloc.pairs_from_retrieval = pairs_from_retrieval
    hloc.reconstruction = reconstruction
    pycolmap = ModuleType("pycolmap")
    pycolmap.CameraMode = SimpleNamespace(SINGLE="single")
    pycolmap.import_images = lambda database, image_dir, mode: calls.append(("import_images", mode))
    monkeypatch.setitem(sys.modules, "hloc", hloc)
    monkeypatch.setitem(sys.modules, "pycolmap", pycolmap)
    monkeypatch.setattr("capture_splat.hloc_runner.hloc_status", lambda: {"ready": True})
    monkeypatch.setattr(
        "capture_splat.hloc_runner.subprocess.run",
        lambda command, text: calls.append(("verify", command[1])) or SimpleNamespace(returncode=0),
    )

    summary = run_hloc_frontend(images, tmp_path / "out", tmp_path / "out/database.db", top_k=32)

    assert calls[:4] == [
        ("extract", "eigenplaces"),
        ("pairs", 32),
        ("extract", "aliked-n16"),
        ("match", "lightglue", "aliked"),
    ]
    assert ("import_images", "single") in calls
    assert ("verify", "matches_importer") in calls
    assert summary["retrieval_top_k"] == 32
    assert summary["camera_mode"] == "single"
