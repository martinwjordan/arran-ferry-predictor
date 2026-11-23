# Implementation plan: Phase 0 and Phase 1 (start the clock)

- **Spec:** `docs/superpowers/specs/2026-11-23-arran-ferry-predictor-design.md` (signed off 23 November 2026)
- **Goal:** live, durable logging of CalMac notices and sailings, AIS positions and Open-Meteo forecasts, running unattended on GitHub Actions, with failures surfaced. No predictions yet.
- **Why first:** the CalMac feed and AIS keep no history. Every day without logging is training data lost.
- **Out of scope here:** notice parser, crossing checks, model, page, Home Assistant (Phase 2 plan, written once data is flowing).

## Conventions

- Python 3.12, package `ferrypredictor` under `src/`, tests under `tests/` with pytest.
- Dependencies: `httpx`, `pydantic` (v2), `pyarrow`, `pyyaml`, `websockets`. Dev: `pytest`, `respx`, `pytest-asyncio`.
- Test first for every unit: write the failing test, run it, implement, run it green, commit.
- All timestamps stored in UTC (ISO 8601 with `Z`, or Arrow timestamp with tz UTC).
- Data files are append-only Parquet, one file per month: `data/<dataset>/YYYY-MM.parquet`. Writes go to a temp file, then `os.replace`, so a crash never leaves a half-written file.
- Small state (last seen notices, last sailing list, discovered MMSIs) lives in `data/state/*.json`, also written atomically.
- `site/health.json` is the only file outside `data/` written in this phase.
- Commit messages end with the attribution lines.
- Local commands for M are PowerShell on Windows. Workflow commands run on GitHub's Ubuntu runners.

## Task 0: Repo and secrets (M)

Create a **public** repo `arran-ferry-predictor` on M's personal GitHub account, empty (no README), then add one secret and one variable.

With the GitHub CLI (PowerShell). `gh secret set` prompts for the value, so the key never appears in history or chat:

```powershell
gh repo create arran-ferry-predictor --public --description "Likelihood of Arran ferry sailings running, up to five days ahead (unofficial)"
gh secret set AISSTREAM_API_KEY --repo <your-github-username>/arran-ferry-predictor
gh variable set CONTACT_EMAIL --repo <your-github-username>/arran-ferry-predictor --body "<contact email for request headers>"
```

Or in the browser: New repository; then Settings, Secrets and variables, Actions: add secret `AISSTREAM_API_KEY` and variable `CONTACT_EMAIL`.

Then, in both cases: Settings, Actions, General, Workflow permissions: **Read and write permissions**, save.

**Done when:** M confirms the repo name. Claude attaches it with push access and pushes the local history (spec and this plan).

## Task 1: Scaffold and config

**Files**
- `pyproject.toml`: project metadata, dependencies above, `[project.optional-dependencies] dev`, pytest config (`pythonpath = ["src"]`, `asyncio_mode = "auto"`).
- `src/ferrypredictor/__init__.py`, `src/ferrypredictor/config.py`.
- `config/routes.yaml`: routes `003` (Ardrossan to Brodick) and `301` (Troon to Brodick) with port names and codes (`GBARD`, `GBBDI`, `GBTRN`); the five forecast points from spec section 5; the AIS bounding box.
- `config/vessels.yaml`: tracked vessel names (CALEDONIAN ISLES, ALFRED, GLEN SANNOX, GLEN ROSA, ISLE OF ARRAN, HEBRIDES, FINLAGGAN, ISLE OF ISLAY), each with optional `mmsi`.
- `README.md`: what it is, unofficial status, data sources with Open-Meteo CC BY 4.0 attribution, CalMac and aisstream.io credits.
- `.gitignore`: `.venv/`, `__pycache__/`, `.pytest_cache/`.
- `.github/workflows/tests.yml`: run pytest on push and pull request.

**Tests** (`tests/test_config.py`)
- `load_routes()` returns codes `{"003", "301"}`, each with two ports.
- `load_points()` returns five named points with latitude and longitude in the Clyde.
- `load_vessels()` upper-cases and strips names; `mmsi` may be absent.

**Local check (M, optional)**

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
pytest
```

**Commit:** "Scaffold package, config and test workflow".

## Task 2: Storage helpers

**File:** `src/ferrypredictor/storage.py`

- `append_rows(dataset_dir, rows, schema, when)`: appends to `YYYY-MM.parquet` for `when`, creating it if missing; validates rows against the explicit `pyarrow` schema; temp file then `os.replace`.
- `read_dataset(dataset_dir)`: concatenates all monthly files (empty table with schema if none).
- `load_state(path, default)` and `save_state(path, obj)`: JSON, atomic.

**Tests** (`tests/test_storage.py`, using `tmp_path`)
- Two appends in the same month produce one file with both rows.
- Appends in different months produce two files; `read_dataset` returns all rows.
- A row missing a required column raises before anything is written; the existing file is unchanged.
- No temp files remain after a successful write.
- `save_state` then `load_state` round-trips; a missing state file returns the default.

**Commit:** "Add atomic Parquet and state storage".

## Task 3: CalMac GraphQL client and models

**Files:** `src/ferrypredictor/calmac.py`; fixtures in `tests/fixtures/calmac/`

**Fixtures:** extracted from `calmac-feed-arran-handover.md` (project doc).
- `routes_2026-11-23.json`: section 4a (which has notice `detail`) merged with section 4c (which has notice `id`, `isActive`, `nextReviewDateTime` and route `vessels`). Notices are in the same order in both; the merge checks titles match before combining.
- `sailings_003_2026-11-23.json` and `sailings_301_2026-11-23.json`: section 4d verbatim. These carry port names only, so `portCode` is optional in the model.

**Client**
- `CalMacClient(contact_email, base_url="https://apim.calmac.co.uk/graphql", timeout=20)` using `httpx`.
- Headers: `Content-Type: application/json`, `User-Agent: arran-ferry-predictor/0.1 (+https://github.com/<owner>/arran-ferry-predictor; <contact_email>)`.
- `query(gql, variables)`: POST; one retry after 5 seconds on network error, 429 or 5xx; on 400, raise `CalMacError` carrying the GraphQL `errors[0].message` (read the body; do not lose it, unlike the HA sensor).
- `fetch_routes(codes)`: the route query from spec section 5, call 1, including `routeStatuses { id title status subStatus detail disruptionReason startDateTime endDateTime nextReviewDateTime updatedAtDateTime isActive }` and `vessels { name vesselCode isActive }`.
- `fetch_sailings(route_code, start_date, end_date)`: `portPairSailings` filtered by `route.routeCode`, `sailingDate` gte start and lt end, ordered by departure; fields `id sailingDate status departureDateTime arrivalDateTime rescheduledDepartureDateTime rescheduledArrivalDateTime originPort { name portCode } destinationPort { name portCode }`.

**Models** (pydantic, strict on the fields we rely on, `extra="ignore"` elsewhere): `Route`, `RouteStatus`, `Vessel`, `Sailing`. Datetimes parsed as UTC.

**Tests** (`tests/test_calmac.py`, `respx`)
- Routes fixture parses to two routes; route 003 has five notices, route 301 has eight; the 301 `SAILING` notice has `disruptionReason == "Weather"`.
- Sailings fixtures parse: 10 sailings for 003 and 6 for 301 on 23 November, all `SCHEDULED`.
- A 400 with an `errors` body raises `CalMacError` whose message contains the GraphQL message.
- 503 then 200 succeeds with exactly two requests; 503 twice raises.
- The request carries the project User-Agent.

**Commit:** "Add CalMac GraphQL client, models and fixtures".

## Task 4: Status and reason normalisation

**File:** `src/ferrypredictor/status.py`

- `normalise_reason(text)`: `None`, `""` and `"None"` become `None`; otherwise lower case and trimmed (`"Crew Availability"` and `"Crew availability"` both give `"crew availability"`).
- `effective_status(route, now)`: worst of `route.status` and the `subStatus` of `SAILING` notices active at `now` (start at or before now, end after now). Order: `NORMAL` < `BE_AWARE` < `DISRUPTIONS` < `ALL_SAILINGS_CANCELLED`.

**Tests**
- At 2026-11-23T12:00Z: route 301 gives `DISRUPTIONS` (route field says `BE_AWARE`); route 003 gives `BE_AWARE`.
- A `SAILING` notice that ended before `now` is ignored.
- Reason normalisation cases above.

**Commit:** "Add effective status and reason normalisation".

## Task 5: Collect job

**File:** `src/ferrypredictor/collect.py`

`run_collect(client, now, data_dir, site_dir)`:
1. Fetch routes `003`, `301`; fetch sailings for each route from today to today plus seven days (UK date).
2. **Notices:** for each notice, key `(id, updatedAtDateTime)`. Keys already in `data/state/notices_seen.json` are skipped; new ones are appended to `data/notices/` with full `detail`, route code, normalised reason and `first_seen_at = now`.
3. **Sailings:** compare with `data/state/sailings_last.json` (keyed on sailing `id`, comparing departure, arrival, rescheduled times and status). Append rows to `data/sailings/` with `change_type` in `new`, `changed` or `removed` (removed only for sailings that departed in the future and vanished), plus `observed_at = now`. Update the state.
4. **Route status:** append to `data/route_status/` only when a route's `(status, effective_status, vessel list)` differs from the last recorded values.
5. Update `site/health.json` for job `collect`: `last_success`, counts written.
6. Return a summary (counts) for the log.

**Tests** (`tests/test_collect.py`, fake client returning fixtures)
- First run: 13 notices written; 16 sailings written as `new`; two route status rows; health updated.
- Second identical run: nothing appended; health still updated.
- Fixture with the 301 `SAILING` notice's `updatedAtDateTime` changed: exactly one new notice row.
- Fixture with the 18:10 Troon sailing removed (before its departure time): one `removed` row.
- A sailing that disappears after it departed writes nothing.

**Commit:** "Add collect job".

## Task 6: AIS listener

**File:** `src/ferrypredictor/ais.py`

- `parse_message(msg, tracked)`: pure. Accepts aisstream `PositionReport` and `ShipStaticData` messages. Uses `MetaData` (`MMSI`, `ShipName`, `latitude`, `longitude`, `time_utc`) and, for position reports, `Sog`, `Cog`, `TrueHeading`, `NavigationalStatus`. Returns a record when the MMSI is known or the normalised name (upper case, trimmed, "MV " prefix removed) is in the tracked list; otherwise `None`.
- `discover(records, state)`: adds name-to-MMSI pairs to `data/state/vessels_mmsi.json`; never overwrites an MMSI set in `config/vessels.yaml`.
- `listen(api_key, bbox, seconds)`: async; connects to `wss://stream.aisstream.io/v0/stream`, sends the subscription (`APIKey`, `BoundingBoxes` as `[[[lat_min, lon_min], [lat_max, lon_max]]]`, `FilterMessageTypes: ["PositionReport", "ShipStaticData"]`) within three seconds of connecting, collects messages for `seconds`, then closes.
- `run_ais(api_key, now, data_dir, site_dir, seconds=180)`: listen, parse, discover, append to `data/ais/`, update health with message counts per vessel. A failed connection raises, so the workflow fails and the repeated-failure check can fire.

**Tests** (`tests/test_ais.py`, synthetic messages in aisstream's format)
- A Caledonian Isles position report parses with speed, position and UTC time.
- An untracked vessel returns `None`.
- `"MV ALFRED  "` normalises to `"ALFRED"`.
- Discovery records a new MMSI and leaves a configured one untouched.
- `run_ais` with a fake `listen` writes records and health.

**Live check:** first manual run of the AIS workflow (Task 9). Expect Caledonian Isles and Alfred within a few runs; if neither appears in a day of runs, report to M before going further.

**Commit:** "Add AIS listener".

## Task 7: Forecast job

**File:** `src/ferrypredictor/forecast.py`

- Forecast request: `https://api.open-meteo.com/v1/forecast` with all five points comma-separated, `hourly=wind_speed_10m,wind_gusts_10m,wind_direction_10m`, `models=ecmwf_ifs`, `wind_speed_unit=kn`, `timezone=GMT`, `forecast_days=6`.
- Marine request: `https://marine-api.open-meteo.com/v1/marine` with the same points, `hourly=wave_height,wave_period,wave_direction`, `timezone=GMT`, `forecast_days=6`. Points over land return nulls; store them as nulls.
- Run time: try Open-Meteo's model metadata for ECMWF IFS to record the model run initialisation time; if unavailable, `run_time` is null and lead time is computed from `fetched_at`. The first live run confirms which applies, and the plan's Task 10 records it.
- Rows: `model`, `run_time`, `fetched_at`, `point`, `valid_time`, `lead_hours`, wind speed, gust, direction, wave height, period, direction.
- Skip writing if the run time equals the last stored run time (no new model run).
- Update health for job `forecast`.

**Tests** (`tests/test_forecast.py`, `respx`, small synthetic responses)
- Five points by six hours gives 30 rows with the right point names and lead hours.
- Marine nulls are kept as nulls.
- Missing metadata falls back to `fetched_at`.
- Same run time as last stored: nothing written.

**Commit:** "Add forecast job".

## Task 8: CLI

**File:** `src/ferrypredictor/__main__.py`

- `python -m ferrypredictor collect | ais | forecast`.
- Reads `CONTACT_EMAIL` and `AISSTREAM_API_KEY` from the environment; a missing required value exits with code 2 and a clear message (never printing secrets).
- Exit code 0 on success, 1 on failure, with the error on stderr.

**Tests:** argument dispatch and missing-environment handling (monkeypatched job functions).

**Commit:** "Add command-line entry point".

## Task 9: Workflows and failure alerts

**Files**
- `scripts/commit_with_retry.sh`: stage `data/` and `site/`; exit quietly if nothing changed; commit as `github-actions[bot]`; `git pull --rebase` and push, up to three attempts with a short back-off.
- `scripts/issue_on_repeated_failure.sh <workflow>`: uses `gh run list` for the last three runs of that workflow; if all failed and no open issue labelled `pipeline-failure` exists, opens one with links to the runs.
- `.github/workflows/collect.yml`: cron `0,20,40 * * * *`; concurrency group `collect`; `permissions: contents: write, issues: write, actions: read`; Python 3.12 with pip cache; run the job; commit; on failure run the issue script. Timeout 10 minutes.
- `.github/workflows/ais.yml`: cron `10,30,50 * * * *` (offset from collect to avoid push races); same pattern; secret `AISSTREAM_API_KEY`. Timeout 10 minutes.
- `.github/workflows/forecast.yml`: cron `15 3,9,15,21 * * *` as a starting point; same pattern. Retimed in Task 10 from observed run times.
- All three also allow `workflow_dispatch` for manual runs.

**Tests:** `tests/test_workflows.py` loads each YAML and checks schedules, concurrency groups, permissions and that secrets are passed only to the job that needs them.

**Commit:** "Add scheduled workflows and failure alerts".

## Task 10: Go live and first-day checks

1. Push to GitHub; confirm the tests workflow passes.
2. Trigger `collect`, `ais` and `forecast` manually; confirm each commits data and updates `site/health.json`.
3. Leave the schedules running. After about 24 hours check:
   - `collect`: around 72 runs, no failures, notices and sailings accumulating.
   - `ais`: MMSIs discovered for the vessels in service; positions logged for each. Copy confirmed MMSIs into `config/vessels.yaml`.
   - `forecast`: whether a model run time was captured; retime the cron to follow it.
4. Update `claude/context-and-data-sources.md` in the project with confirmed MMSIs, forecast timing and any feed surprises.

**Done when:** all three jobs have run unattended for 24 hours with data committed and no open `pipeline-failure` issue.

## Task 11: One-week review

After seven days of logging: AIS coverage (share of timetabled crossings with at least one mid-channel fix), any CalMac errors, repo size growth, and the notices collected so far. Findings feed the Phase 2 plan (parser, crossing checks, baseline predictions, page and Home Assistant).

## Build notes (23 November 2026)

Changes made during the build, with reasons:

- **Write-once part files, not monthly files.** `data/<dataset>/YYYY-MM/<timestamp>.parquet`, one per run. Rewriting a growing monthly file every 20 minutes would make git store a new copy each time (gigabytes a year).
- **Data on a `data` branch.** Jobs check out `main` for code and a sparse, shallow copy of `data` (state and health only) for writing. Keeps `main` history readable and stops data commits triggering tests.
- **One health file per job** (`site/health/<job>.json`) so jobs finishing together never edit the same file.
- **Fixtures captured live** from CalMac at 12:26 on 23 November rather than copied from the handover pack. They include notice `id` and `detail` in one response. The 301 notice had been reworded at 12:18 (same outcomes), a real example of a notice version change.
- **Forecast timing from evidence.** Open-Meteo's metadata shows the ECMWF IFS 00Z run available at 06:35 UTC (about 6.6 hours after initialisation, 6-hourly updates), so `forecast` runs at 00:50, 06:50, 12:50 and 18:50 UTC. Run times come from `/data/<model>/static/meta.json`.
- **UK Met Office 2 km added.** Open-Meteo carries `ukmo_uk_deterministic_2km` (hourly updates, about 49 hours ahead), logged alongside ECMWF.
- **Open-Meteo rate limits.** The free tier is limited per IP and returned 429 "Daily API request limit exceeded" from this build environment's shared address. GitHub runners use different addresses; the job retries once, writes whatever succeeded, and fails loudly otherwise. If 429s persist on runners, the fallback is an Open-Meteo API key.
- **Live checks passed** for the CalMac client, collect job (13 notices and 120 sailings on the first run, nothing on an identical second run) and forecast job (660 ECMWF rows, 245 UK Met Office rows, 660 marine rows). AIS is checked on the first live run, as the key lives only in GitHub.
