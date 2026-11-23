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
DEC = datetime(2026, 12, 1, 9, 0, tzinfo=timezone.utc)


def row(i, at=NOV, value=1):
    return {"id": str(i), "value": value, "at": at}


def test_two_appends_in_same_month_share_one_file(tmp_path):
    storage.append_rows(tmp_path, [row(1)], SCHEMA, NOV)
    storage.append_rows(tmp_path, [row(2)], SCHEMA, NOV)
    files = sorted(p.name for p in tmp_path.iterdir())
    assert files == ["2026-11.parquet"]
    assert storage.read_dataset(tmp_path, SCHEMA).column("id").to_pylist() == ["1", "2"]


def test_appends_in_different_months_make_two_files(tmp_path):
    storage.append_rows(tmp_path, [row(1)], SCHEMA, NOV)
    storage.append_rows(tmp_path, [row(2, at=DEC)], SCHEMA, DEC)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["2026-11.parquet", "2026-12.parquet"]
    assert storage.read_dataset(tmp_path, SCHEMA).num_rows == 2


def test_missing_required_column_raises_and_leaves_file_untouched(tmp_path):
    storage.append_rows(tmp_path, [row(1)], SCHEMA, NOV)
    before = (tmp_path / "2026-11.parquet").read_bytes()
    with pytest.raises(ValueError, match="id"):
        storage.append_rows(tmp_path, [{"value": 2, "at": NOV}], SCHEMA, NOV)
    assert (tmp_path / "2026-11.parquet").read_bytes() == before


def test_no_temp_files_left_behind(tmp_path):
    storage.append_rows(tmp_path, [row(1)], SCHEMA, NOV)
    assert not list(tmp_path.glob("*.tmp"))


def test_empty_rows_write_nothing(tmp_path):
    assert storage.append_rows(tmp_path, [], SCHEMA, NOV) is None
    assert not list(tmp_path.iterdir())


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
