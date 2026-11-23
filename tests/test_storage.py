from datetime import datetime, timezone

import pyarrow as pa
import pytest

from ferrypredictor import storage

SCHEMA = pa.schema(
    [
        pa.field("id", pa.string(), nullable=False),
        pa.field("value", pa.int64()),
        pa.field("at", pa.timestamp("us", tz="UTC"), nullable=False),
    ]
)
NOV = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)
NOV_LATER = datetime(2026, 11, 23, 12, 20, tzinfo=timezone.utc)
DEC = datetime(2026, 12, 1, 9, 0, tzinfo=timezone.utc)


def row(i, at=NOV, value=1):
    return {"id": str(i), "value": value, "at": at}


def parts(root):
    return sorted(str(p.relative_to(root)) for p in root.rglob("*.parquet"))


def test_each_append_writes_a_new_part_in_its_month(tmp_path):
    storage.append_rows(tmp_path, [row(1)], SCHEMA, NOV)
    storage.append_rows(tmp_path, [row(2)], SCHEMA, NOV_LATER)
    storage.append_rows(tmp_path, [row(3, at=DEC)], SCHEMA, DEC)
    assert parts(tmp_path) == [
        "2026-11/20261123T120000Z.parquet",
        "2026-11/20261123T122000Z.parquet",
        "2026-12/20261201T090000Z.parquet",
    ]
    assert storage.read_dataset(tmp_path, SCHEMA).column("id").to_pylist() == ["1", "2", "3"]


def test_existing_parts_are_never_modified(tmp_path):
    first = storage.append_rows(tmp_path, [row(1)], SCHEMA, NOV)
    before = first.read_bytes()
    storage.append_rows(tmp_path, [row(2)], SCHEMA, NOV)  # same second
    assert first.read_bytes() == before
    assert len(parts(tmp_path)) == 2


def test_missing_required_column_raises_and_writes_nothing(tmp_path):
    with pytest.raises(ValueError, match="id"):
        storage.append_rows(tmp_path, [{"value": 2, "at": NOV}], SCHEMA, NOV)
    assert parts(tmp_path) == []


def test_no_temp_files_left_behind(tmp_path):
    storage.append_rows(tmp_path, [row(1)], SCHEMA, NOV)
    assert not list(tmp_path.rglob("*.tmp"))


def test_empty_rows_write_nothing(tmp_path):
    assert storage.append_rows(tmp_path, [], SCHEMA, NOV) is None
    assert parts(tmp_path) == []


def test_read_dataset_when_empty_returns_schema(tmp_path):
    table = storage.read_dataset(tmp_path / "missing", SCHEMA)
    assert table.num_rows == 0
    assert table.schema.names == SCHEMA.names


def test_state_round_trip_and_default(tmp_path):
    path = tmp_path / "state" / "thing.json"
    assert storage.load_state(path, {"a": 0}) == {"a": 0}
    storage.save_state(path, {"a": 1, "b": [1, 2]})
    assert storage.load_state(path, {}) == {"a": 1, "b": [1, 2]}
    assert not list(path.parent.glob("*.tmp"))


def test_unchanged_state_is_not_rewritten(tmp_path):
    path = tmp_path / "state.json"
    storage.save_state(path, {"a": 1})
    mtime = path.stat().st_mtime_ns
    storage.save_state(path, {"a": 1})
    assert path.stat().st_mtime_ns == mtime
