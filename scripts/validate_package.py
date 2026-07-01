#!/usr/bin/env python3
import argparse
from pathlib import Path
from capture_splat.capture_schema import load_capture

parser = argparse.ArgumentParser(description="Validate a Capture Splat export folder.")
parser.add_argument("--capture", type=Path, required=True)
args = parser.parse_args()
data = load_capture(args.capture)
print(f"OK: {len(data['frames'])} frames, schema={data['schema']}")
