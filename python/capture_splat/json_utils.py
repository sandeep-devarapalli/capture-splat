from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any


def reject_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def load_json_strict(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"), parse_constant=reject_constant)


def ensure_finite(value: Any, path: str = "$") -> None:
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"non-finite JSON value at {path}: {value}")
    elif isinstance(value, dict):
        for key, item in value.items():
            ensure_finite(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            ensure_finite(item, f"{path}[{index}]")


def write_json_strict(path: Path, payload: Any) -> None:
    ensure_finite(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, sort_keys=True, allow_nan=False)
    json.loads(text, parse_constant=reject_constant)
    path.write_text(text + "\n", encoding="utf-8")
