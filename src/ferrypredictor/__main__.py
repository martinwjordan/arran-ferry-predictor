"""Command line: python -m ferrypredictor collect | ais | forecast"""

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

REQUIRED = {"collect": ["CONTACT_EMAIL"], "ais": ["AISSTREAM_API_KEY"], "forecast": ["CONTACT_EMAIL"]}


def _collect(env: dict, now: datetime) -> dict:
    from .calmac import CalMacClient
    from .collect import run_collect

    return run_collect(CalMacClient(env["CONTACT_EMAIL"]), now, DATA_DIR, SITE_DIR)


def _ais(env: dict, now: datetime) -> dict:
    from .ais import run_ais

    return run_ais(env["AISSTREAM_API_KEY"], now, DATA_DIR, SITE_DIR)


def _forecast(env: dict, now: datetime) -> dict:
    from .forecast import run_forecast

    return run_forecast(env["CONTACT_EMAIL"], now, DATA_DIR, SITE_DIR)


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
    job = {"collect": _collect, "ais": _ais, "forecast": _forecast}[args.job]
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
