from __future__ import annotations

import argparse
import json
from pathlib import Path

from .colmap_export import export_colmap_text
from .ingest import ingest_capture
from .vksplat_runner import doctor, run_vksplat


def main() -> None:
    parser = argparse.ArgumentParser(prog="capture-splat")
    sub = parser.add_subparsers(dest="command", required=True)
    p_ingest = sub.add_parser("ingest", help="Normalize capture export")
    p_ingest.add_argument("--capture", type=Path, required=True)
    p_ingest.add_argument("--out", type=Path, required=True)
    p_colmap = sub.add_parser("colmap-export", help="Write COLMAP text package")
    p_colmap.add_argument("--capture", type=Path, required=True)
    p_colmap.add_argument("--out", type=Path, required=True)
    p_train = sub.add_parser("train-vksplat", help="Run VkSplat on a COLMAP package")
    p_train.add_argument("--package", type=Path, required=True)
    p_train.add_argument("--out", type=Path, required=True)
    p_train.add_argument("--vksplat-root", type=Path, required=True)
    p_train.add_argument("--steps", type=int, default=30000)
    p_train.add_argument("--dry-run", action="store_true")
    p_doctor = sub.add_parser("doctor", help="Check local runtime tools")
    p_doctor.add_argument("--vksplat-root", type=Path)
    args = parser.parse_args()
    if args.command == "ingest":
        payload = ingest_capture(args.capture, args.out)
    elif args.command == "colmap-export":
        payload = export_colmap_text(args.capture, args.out)
    elif args.command == "train-vksplat":
        payload = run_vksplat(args.package, args.out, args.vksplat_root, steps=args.steps, dry_run=args.dry_run)
    elif args.command == "doctor":
        payload = doctor(args.vksplat_root)
    else:
        raise AssertionError(args.command)
    print(json.dumps(payload, indent=2))
