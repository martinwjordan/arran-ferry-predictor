"""Command line: python -m ferrypredictor collect | parse | predict | labels | score | ais | forecast | backfill | site"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

from pathlib import Path

from . import health
from .config import REPO_ROOT

DATA_DIR = Path(os.environ.get("FERRY_DATA_DIR", REPO_ROOT / "data"))
SITE_DIR = Path(os.environ.get("FERRY_SITE_DIR", REPO_ROOT / "site"))
PUBLISH_DIR = Path(os.environ.get("FERRY_PUBLISH_DIR", REPO_ROOT / "public"))

REQUIRED = {
    "collect": ["CONTACT_EMAIL"],
    "ais": ["AISSTREAM_API_KEY"],
    "forecast": ["CONTACT_EMAIL"],
    "backfill": ["CONTACT_EMAIL"],
    "parse": [],
    "site": [],
    "predict": [],
    "labels": [],
    "score": [],
}


def _collect(env: dict, now: datetime) -> dict:
    from .calmac import CalMacClient
    from .collect import run_collect

    return run_collect(CalMacClient(env["CONTACT_EMAIL"]), now, DATA_DIR, SITE_DIR)


def _ais(env: dict, now: datetime) -> dict:
    from .ais import bbox_override, run_ais

    seconds = float(os.environ.get("FERRY_AIS_SECONDS") or 180)
    bbox = bbox_override(os.environ.get("FERRY_AIS_BBOX"))
    return run_ais(env["AISSTREAM_API_KEY"], now, DATA_DIR, SITE_DIR, seconds=seconds, bbox=bbox)


def _forecast(env: dict, now: datetime) -> dict:
    from .forecast import run_forecast

    return run_forecast(env["CONTACT_EMAIL"], now, DATA_DIR, SITE_DIR)


def _backfill(env: dict, now: datetime) -> dict:
    from .backfill import run_backfill

    chunks = int(os.environ.get("FERRY_BACKFILL_CHUNKS") or 40)
    return run_backfill(env["CONTACT_EMAIL"], now, DATA_DIR, SITE_DIR, max_chunks=chunks)


def _parse(env: dict, now: datetime) -> dict:
    from .parse import run_parse

    return run_parse(now, DATA_DIR, SITE_DIR)


def _site(env: dict, now: datetime) -> dict:
    from .site import run_site

    return run_site(now, DATA_DIR, SITE_DIR, PUBLISH_DIR)


def _predict(env: dict, now: datetime) -> dict:
    from .predict import run_predict

    return run_predict(now, DATA_DIR, SITE_DIR)


def _labels(env: dict, now: datetime) -> dict:
    from .labels import run_labels

    return run_labels(now, DATA_DIR, SITE_DIR)


def _score(env: dict, now: datetime) -> dict:
    from .score import run_score

    return run_score(now, DATA_DIR, SITE_DIR)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ferrypredictor")
    parser.add_argument("job", choices=sorted(REQUIRED))
    args = parser.parse_args(argv)

    missing = [k for k in REQUIRED[args.job] if not os.environ.get(k)]
    if missing:
        print(f"Missing environment variable(s): {', '.join(missing)}", file=sys.stderr)
        return 2
    env = {k: os.environ[k] for k in REQUIRED[args.job]}
    now = datetime.now(timezone.utc)
    job = {"collect": _collect, "ais": _ais, "forecast": _forecast, "backfill": _backfill, "parse": _parse, "site": _site, "predict": _predict,
           "labels": _labels, "score": _score}[args.job]
    try:
        summary = job(env, now)
    except Exception as e:  # report and fail the run; the workflow raises an issue on repeats
        message = f"{type(e).__name__}: {e}"
        print(f"{args.job} failed: {message}", file=sys.stderr)
        try:
            health.update_health(SITE_DIR, args.job, now, ok=False, details={"error": message[:300]})
        except Exception:
            pass
        return 1
    print(json.dumps({"job": args.job, "at": now.isoformat(timespec="seconds"), **summary}, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
