#!/usr/bin/env python3
from capture_splat.vksplat_runner import doctor
from capture_splat.json_utils import write_json_strict
import argparse
import json
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--vksplat-root", type=Path)
parser.add_argument("--summary-out", type=Path)
args = parser.parse_args()
payload = doctor(args.vksplat_root)
print(json.dumps(payload, indent=2))
if args.summary_out:
    write_json_strict(args.summary_out, payload)
