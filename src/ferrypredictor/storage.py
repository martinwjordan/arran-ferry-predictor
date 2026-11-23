"""Append-only Parquet datasets and small JSON state files, all written atomically.

Each write creates a new, never-modified part file: <dataset>/YYYY-MM/YYYYMMDDTHHMMSSZ.parquet.
Rewriting a growing file on every run would make git store a fresh copy each time; immutable
parts keep repository growth equal to the data actually collected.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq


def part_path(dataset_dir: Path, when: datetime) -> Path:
    when = when.astimezone(timezone.utc)
    base = Path(dataset_dir) / f"{when:%Y-%m}" / f"{when:%Y%m%dT%H%M%SZ}"
    path, n = base.with_suffix(".parquet"), 1
    while path.exists():
        path = base.with_name(f"{base.name}-{n}.parquet")
        n += 1
    return path


def _check_required(rows: list[dict], schema: pa.Schema) -> None:
    required = [f.name for f in schema if not f.nullable]
    for i, r in enumerate(rows):
        missing = [name for name in required if r.get(name) is None]
        if missing:
            raise ValueError(f"row {i} is missing required field(s): {', '.join(missing)}")


def append_rows(dataset_dir: Path, rows: list[dict], schema: pa.Schema, when: datetime) -> Path | None:
    """Write rows as a new part file. Returns its path, or None if there was nothing to write."""
    if not rows:
        return None
    _check_required(rows, schema)
    table = pa.Table.from_pylist(rows, schema=schema)
    path = part_path(dataset_dir, when)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    pq.write_table(table, tmp, compression="zstd")
    os.replace(tmp, path)
    return path


def read_dataset(dataset_dir: Path, schema: pa.Schema) -> pa.Table:
    dataset_dir = Path(dataset_dir)
    files = sorted(dataset_dir.rglob("*.parquet")) if dataset_dir.exists() else []
    if not files:
        return schema.empty_table()
    return pa.concat_tables([_conform(pq.read_table(f), schema) for f in files])


def _conform(table: pa.Table, schema: pa.Schema) -> pa.Table:
    """Fit an older part to the current schema: add missing columns as nulls, drop unknown ones."""
    for field in schema:
        if field.name not in table.column_names:
            table = table.append_column(field.name, pa.nulls(table.num_rows, type=field.type))
    return table.select(schema.names).cast(schema)


def load_state(path: Path, default: Any) -> Any:
    path = Path(path)
    if not path.exists():
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_state(path: Path, obj: Any) -> None:
    """Write JSON atomically; skip the write when the content is unchanged (keeps git quiet)."""
    path = Path(path)
    text = json.dumps(obj, indent=1, sort_keys=True, default=str, ensure_ascii=False) + "\n"
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
