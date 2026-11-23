"""site/health.json: last success (and failure) per job, read by the page and Home Assistant."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .storage import load_state, save_state


def update_health(site_dir: Path, job: str, now: datetime, ok: bool = True, details: dict | None = None) -> dict:
    path = Path(site_dir) / "health.json"
    health = load_state(path, {"jobs": {}})
    entry = health["jobs"].setdefault(job, {})
    stamp = now.isoformat(timespec="seconds")
    if ok:
        entry["last_success"] = stamp
        entry["details"] = details or {}
    else:
        entry["last_failure"] = stamp
        entry["error"] = (details or {}).get("error")
    health["updated"] = stamp
    save_state(path, health)
    return health
