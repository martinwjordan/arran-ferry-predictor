# Arran ferry predictor

**Unofficial.** Estimates how likely each CalMac sailing between Arran and the mainland (Ardrossan to Brodick and Troon to Brodick, both ways) is to run, up to five days ahead. It combines weather forecasts with CalMac's own service history, and learns from what actually sails.

Always check [CalMac's service status](https://www.calmac.co.uk/en-gb/service-status/) before travelling. This project is not affiliated with CalMac.

## Status

Phase 1: collecting data. Predictions and the public page come next.

## How it works

Scheduled GitHub Actions jobs:

| Job | Schedule | What it does |
|---|---|---|
| `collect` | Every 20 minutes | Logs CalMac route status, notices and the timetable for the next week |
| `ais` | Paused | Listens to AIS ship positions around Arran for three minutes. Paused on 23 November 2026: aisstream.io has no receiver coverage of the Arran crossings. Re-enable from the Actions tab once a covering source exists. |
| `forecast` | Four times a day | Logs wind and wave forecasts for the ports and mid-channel |

Scheduling: GitHub has not been firing the cron schedules reliably for this repo, so the jobs are started by an outside timer (cron-job.org, live since 23 November 2026; its token expires 22 November 2027). See [docs/scheduling.md](docs/scheduling.md).

Collected data lives on the [`data` branch](../../tree/data), so the code history on `main` stays readable:

- `data/<dataset>/YYYY-MM/*.parquet`: one write-once Parquet file per run (`notices`, `sailings`, `route_status`, `ais`, `forecasts`).
- `data/state/`: small JSON files the jobs use to spot changes and discovered vessel MMSIs.
- `site/health/<job>.json`: last success or failure for each job.

If a job fails three runs in a row, it opens a GitHub issue labelled `pipeline-failure`.

The design is in [`docs/superpowers/specs/`](docs/superpowers/specs/) and the build plans in [`docs/superpowers/plans/`](docs/superpowers/plans/).

## Data sources

- Service status and timetables: CalMac's public website feed.
- Weather and marine forecasts: [Open-Meteo](https://open-meteo.com/), licensed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).
- Ship positions: [aisstream.io](https://aisstream.io/).

## Running locally (Windows PowerShell)

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
pytest
```

## Licence

Code: [MIT](LICENSE). Forecast data from Open-Meteo is CC BY 4.0; CalMac and AIS data remain subject to their providers' terms.
