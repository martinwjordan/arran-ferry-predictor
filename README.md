# Arran ferry predictor

**Unofficial.** Estimates how likely each CalMac sailing between Arran and the mainland (Ardrossan to Brodick and Troon to Brodick, both ways) is to run, up to five days ahead. It combines weather forecasts with CalMac's own service history, and learns from what actually sails.

Always check [CalMac's service status](https://www.calmac.co.uk/en-gb/service-status/) before travelling. This project is not affiliated with CalMac.

## Status

Predicting. Every sailing in the next five days gets a chance it runs, refreshed every 20 minutes. The model meets its launch bar on days 1 to 3 in backtests over the last two winters. Live labels and a scorecard check the chances against what actually happened. The phone page runs on GitHub Pages, unlisted while it proves itself.

## How it works

Scheduled GitHub Actions jobs:

| Job | Schedule | What it does |
|---|---|---|
| `collect` | Every 20 minutes | Logs CalMac route status, notices and the timetable for the next week |
| `parse` | After each `collect` | Turns CalMac SAILING notices into per-sailing events (`cancelled`, `diverted`, `at_risk`, `at_risk_diversion`, `reinstated`, `review_at`) in `data/events/`; anything it can't resolve goes to `data/unparsed/` for review, never guessed |
| `ais` | Paused | Listens to AIS ship positions around Arran for three minutes. Paused on 23 November 2026: aisstream.io has no receiver coverage of the Arran crossings. Re-enable from the Actions tab once a covering source exists. |
| `predict` | After each `parse` | Chance each sailing runs, from the latest GFS and ECMWF 0.25° runs and `model/weather.json`; CalMac cancellations show as 0. Also a red, amber or green light per sailing for livestock and timber (`cargo.py`): CalMac's own wording where its notices say ("No livestock or timber will be carried", "case-by-case basis"), otherwise an estimate from the chance it runs. Writes `site/predictions.json` (page and [Home Assistant](docs/home-assistant.md)) and archives one row per sailing per forecast run in `data/predictions/` for scoring |
| `labels` | After each `predict` | Once a UK day has settled (end of the following day), labels every sailing from CalMac's notices first, then timetable removals, else operated, in the same shape as the FOI labels plus a confidence. Planned changes and late-added relief sailings are kept but not trained on or scored. One part per day in `data/labels_live/` |
| `score` | After each `labels` | The model's chance from the latest run at each lead day against whether the sailing ran: Brier against "assume it sails" and climatology, AUC, bands, confident labels separately, and the misses. `site/scorecard.json` and `site/scorecard.md` |
| `forecast` | Four times a day | Logs wind and wave forecasts for the ports and mid-channel |
| `archive` | After each `forecast` | Extends the GFS and ECMWF 0.25° training archive to two days ago, so live labels have matching forecasts for retraining |
| `publish` | After each successful `collect` (dispatched by it) | Rebuilds the phone page (`web/index.html` plus `sailings.json`) and deploys it to GitHub Pages: every sailing for the next five days with the chance it runs, CalMac's word on it, the livestock and timber light, and the forecast wind and waves mid-crossing. |
| `backfill` | By hand, until complete | Fetches archived forecasts as issued 1 to 5 days ahead (GFS, ECMWF, UK Met Office; January 2024 to 23 November 2026, where each model's archive allows) into `data/forecast_archive/`, for training. Resumes where the last run stopped; progress in `site/health/backfill.json`. |

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
- Ship positions: [aisstream.io](https://aisstream.io/) (paused).
- Training labels: CalMac per-sailing records released under FOI (kept in a private repository).

## Running locally (Windows PowerShell)

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
pytest
```

## Historical outcomes (CalMac FOI data)

CalMac's per-sailing performance data (FOI 4405, released via FOI 4428: every scheduled Arran sailing from 2007 to September 2026, with vessel, outcome and reason) is the training set. The files are CalMac's, so they live in a private data repository, not here. This repo holds only the code that turns them into labels: `python -m ferrypredictor.foi ANNEX_E.xlsx [ANNEX_D.csv] OUT.parquet` (needs `pip install -e ".[foi]"`), tested on made-up rows.

The model (`features.py`, `model.py`) and its backtest (`python -m ferrypredictor.backtest LABELS.parquet ARCHIVE_DIR OUT.md`, needs `.[model]`) also live here. The backtest retrains monthly, as the live system does, and reports aggregate scores only.

`python -m ferrypredictor.train FOI_LABELS.parquet data/labels_live ARCHIVE_DIR model/weather.json` fits the live model (any number of label sources; FOI wins where both cover a sailing) and writes it as JSON: per lead day, the logistic coefficients, wind-rule rates and stacking weights, plus each route's and ferry's rate of technical and other cancellations over the last 180 days. Coefficients and aggregate rates only, no sailing rows. The predict job needs only numpy and pandas to use it. It is retrained monthly from the private data repository.

The chance it runs is (1 − weather risk) × (1 − technical and other risk). Today and the next two days show a percentage; days 3 and 4 show a band (Likely 75% or more, Uncertain, Unlikely under 40%).

## Licence

Code: [MIT](LICENSE). Forecast data from Open-Meteo is CC BY 4.0; CalMac and AIS data remain subject to their providers' terms.
