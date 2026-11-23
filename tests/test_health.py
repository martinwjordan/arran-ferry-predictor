import json
from datetime import datetime, timezone

from ferrypredictor.health import update_health

T1 = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)
T2 = datetime(2026, 11, 23, 12, 20, tzinfo=timezone.utc)


def test_success_then_failure_keeps_last_success(tmp_path):
    update_health(tmp_path, "collect", T1, details={"notices": 3})
    update_health(tmp_path, "collect", T2, ok=False, details={"error": "HTTP 503"})
    h = json.loads((tmp_path / "health.json").read_text())
    job = h["jobs"]["collect"]
    assert job["last_success"] == "2026-11-23T12:00:00+00:00"
    assert job["last_failure"] == "2026-11-23T12:20:00+00:00"
    assert job["error"] == "HTTP 503"
    assert job["details"] == {"notices": 3}


def test_jobs_are_independent(tmp_path):
    update_health(tmp_path, "collect", T1)
    update_health(tmp_path, "ais", T2)
    h = json.loads((tmp_path / "health.json").read_text())
    assert set(h["jobs"]) == {"collect", "ais"}
    assert h["updated"] == "2026-11-23T12:20:00+00:00"
