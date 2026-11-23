from datetime import datetime, timezone

from ferrypredictor.health import health_path, read_health, update_health

T1 = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)
T2 = datetime(2026, 11, 23, 12, 20, tzinfo=timezone.utc)


def test_success_then_failure_keeps_last_success(tmp_path):
    update_health(tmp_path, "collect", T1, details={"notices": 3})
    update_health(tmp_path, "collect", T2, ok=False, details={"error": "HTTP 503"})
    job = read_health(tmp_path, "collect")
    assert job["last_success"] == "2026-11-23T12:00:00+00:00"
    assert job["last_failure"] == "2026-11-23T12:20:00+00:00"
    assert job["error"] == "HTTP 503"
    assert job["details"] == {"notices": 3}
    assert job["updated"] == "2026-11-23T12:20:00+00:00"


def test_each_job_has_its_own_file(tmp_path):
    update_health(tmp_path, "collect", T1)
    update_health(tmp_path, "ais", T2)
    assert health_path(tmp_path, "collect").exists()
    assert health_path(tmp_path, "ais").exists()
    assert read_health(tmp_path, "collect")["job"] == "collect"
