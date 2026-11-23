"""site/health/<job>.json: last success (and failure) per job, read by the page and Home Assistant.

One file per job, so jobs finishing at the same time never edit the same file.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .storage import load_state, save_state


def health_path(site_dir: Path, job: str) -> Path:
    return Path(site_dir) / "health" / f"{job}.json"


def read_health(site_dir: Path, job: str) -> dict:
    return load_state(health_path(site_dir, job), {})


def update_health(site_dir: Path, job: str, now: datetime, ok: bool = True, details: dict | None = None) -> dict:
    path = health_path(site_dir, job)
    entry = load_state(path, {"job": job})
    stamp = now.isoformat(timespec="seconds")
    if ok:
        entry["last_success"] = stamp
        entry["details"] = details or {}
    else:
        entry["last_failure"] = stamp
        entry["error"] = (details or {}).get("error")
    entry["updated"] = stamp
    save_state(path, entry)
    return entry
