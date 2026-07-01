from pathlib import Path


def test_no_generated_3d_artifacts_committed() -> None:
    root = Path(__file__).resolve().parents[1]
    forbidden = {".ply", ".las", ".laz", ".npy", ".npz", ".ipa", ".xcarchive"}
    skipped_dirs = {".git", ".venv", "__pycache__", ".pytest_cache", "build", "dist", "external", "runs", "captures"}
    offenders = []
    for path in root.rglob("*"):
        if any(part in skipped_dirs for part in path.parts):
            continue
        if path.is_file() and path.suffix.lower() in forbidden:
            offenders.append(path)
    assert offenders == []
