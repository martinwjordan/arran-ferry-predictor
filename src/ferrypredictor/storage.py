"""Append-only monthly Parquet datasets and small JSON state files, all written atomically."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq


def month_file(dataset_dir: Path, when: datetime) -> Path:
    return Path(dataset_dir) / f"{when:%Y-%m}.parquet"


def _check_required(rows: list[dict], schema: pa.Schema) -> None:
    required = [f.name for f in schema if not f.nullable]
    for i, r in enumerate(rows):
        missing = [name for name in required if r.get(name) is None]
        if missing:
            raise ValueError(f"row {i} is missing required field(s): {', '.join(missing)}")


def append_rows(dataset_dir: Path, rows: list[dict], schema: pa.Schema, when: datetime) -> Path | None:
    """Append rows to the month file for `when`. Returns the file path, or None if there was nothing to write."""
    if not rows:
        return None
    _check_required(rows, schema)
    new = pa.Table.from_pylist(rows, schema=schema)
    dataset_dir = Path(dataset_dir)
    dataset_dir.mkdir(parents=True, exist_ok=True)
    path = month_file(dataset_dir, when)
    if path.exists():
        existing = pq.read_table(path).cast(schema)
        new = pa.concat_tables([existing, new])
    tmp = path.with_name(path.name + ".tmp")
    pq.write_table(new, tmp, compression="zstd")
    os.replace(tmp, path)
    return path


def read_dataset(dataset_dir: Path, schema: pa.Schema) -> pa.Table:
    files = sorted(Path(dataset_dir).glob("*.parquet")) if Path(dataset_dir).exists() else []
    if not files:
        return schema.empty_table()
    return pa.concat_tables([pq.read_table(f).cast(schema) for f in files])


def load_state(path: Path, default: Any) -> Any:
    path = Path(path)
    if not path.exists():
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_state(path: Path, obj: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1, sort_keys=True, default=str, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)
