# Arran ferry predictor: design spec

- **Date:** 23 November 2026
- **Owner:** Martin Jordan (M), Lamlash
- **Status:** Approved in brainstorming, awaiting written-spec review
- **Companion doc:** `claude/context-and-data-sources.md` (project), holding source research and fleet notes

## 1. Summary

A scheduled tool that estimates, for every timetabled Arran sailing over the next five days, the chance it sails as planned from the planned port. It covers Ardrossan to Brodick and Troon to Brodick, both directions. It combines weather forecasts with CalMac's own service history, follows each vessel's rotation so cancellations carry forward, and learns from what actually sails.

It runs as a deterministic Python pipeline on GitHub Actions. A daily Claude scheduled task adds judgement on non-weather causes. Results appear on a phone-first web page (GitHub Pages) and on M's Home Assistant dashboard. If it proves accurate over a winter, M intends to share it with the island community.

## 2. Goals and non-goals

**Goals**

1. Per-sailing likelihood for every scheduled sailing up to five days ahead, for both Arran routes and both directions.
2. Capture knock-on effects across vessel rotations and across day boundaries (for example, a cancelled last sailing from Troon cancelling the first sailing from Brodick next morning).
3. Account for non-weather causes: vessel faults, port and operational issues, staffing.
4. Learn over time from logged outcomes, and measure accuracy honestly against simple benchmarks.
5. Usable anywhere: no dependency on Home Assistant being reachable remotely.
6. Ready to share publicly without rework once accurate.

**Non-goals for version one**

- Push alerts or notifications (Home Assistant can add these later from its sensor).
- Other routes, including Claonaig to Lochranza.
- Booking availability or capacity.
- AIS-based verification of sailings (candidate for later).
- XC Weather integration.

## 3. Key constraints and context

- **Fleet changes often.** Caledonian Isles (Ardrossan) is due to retire in February; Glen Rosa is due on Troon to Brodick from February; Hebrides and Finlaggan are expected at Ardrossan later. Vessel and port are first-class inputs, and rotations come from the timetable rather than code.
- **Crossing times:** about 55 minutes from Ardrossan and 75 minutes from Troon.
- **Ground truth:** CalMac's per-sailing status feed (the same one M's Home Assistant sensor uses) for live outcomes; CalMac FOI releases for history.
- **Forecasts:** Open-Meteo (free, no authentication, CC BY 4.0), including archived model runs that allow backfill from March 2024 without look-ahead.

## 4. Architecture

```
Public GitHub repo: arran-ferry-predictor (M's personal account)
 ├─ collect    every 20 min          CalMac status, both routes   → data/sailings/
 ├─ forecast   4× daily              Open-Meteo wind and waves    → data/forecasts/
 ├─ timetable  daily                 next 5 days of sailings      → data/timetable/
 ├─ predict    after forecast, and   per-sailing probabilities    → site/predictions.json
 │             after any status change
 ├─ train      weekly                refit and backtest           → models/, site/accuracy.json
 └─ publish    after predict         static page                  → GitHub Pages

Claude daily review (05:45 UK)  reads notices and news → commits adjustments.json and outlook.md
Home Assistant                  reads site/predictions.json every 15 min
```

**Principles**

- Each job does one thing and talks to the others only through files in the repo.
- Claude never publishes and never edits the model. It commits adjustments with reasons and expiry dates; the pipeline applies them and scores raw and adjusted predictions separately.
- Jobs write to separate paths, so they never need to block each other. Each commit rebases and retries (up to three times) on a push conflict. Runs of the same job share a concurrency group so they never overlap.
- Outputs are built in full, then swapped in. A failed run leaves the last good version live.

## 5. Components

| Component | Trigger | Input | Output |
|---|---|---|---|
| `collect` | Every 20 minutes | CalMac status feed for routes `003` and `301` | Appends changed sailing states to `data/sailings/YYYY-MM.parquet`; updates `health.json` |
| `forecast` | Four times daily, timed to follow Open-Meteo's ECMWF updates | Open-Meteo Forecast and Marine APIs for five points | Appends a full run to `data/forecasts/YYYY-MM.parquet` |
| `timetable` | Daily, and on detected timetable change | CalMac feed (preferred) or published timetable PDFs | `data/timetable/current.parquet`: sailings for the next five days with vessel |
| `predict` | After `forecast`, and after `collect` records a status change | Latest forecast, timetable, sailing log, model, `adjustments.json` | `site/predictions.json` |
| `train` | Weekly, Sunday 03:00 UTC | All logs plus backfill | `models/current.*`, `site/accuracy.json`, a backtest report |
| `publish` | After `predict` | `predictions.json`, `accuracy.json`, `outlook.md` | Static site on GitHub Pages |
| Claude review | Claude scheduled task, daily 05:45 UK | Predictions, CalMac notices and customer updates, recent news | Commits `adjustments.json` and `outlook.md` |
| HA sensor | Every 15 minutes | Public `predictions.json` URL | Home Assistant sensors and dashboard card (YAML supplied; installed via the home automation project) |

**Forecast points (approximate, 2 dp):** Ardrossan 55.64N 4.82W; Troon 55.55N 4.69W; Brodick 55.58N 5.14W; mid-channel Ardrossan route 55.61N 4.98W; mid-channel Troon route 55.56N 4.91W.

**Polite polling:** every request carries a User-Agent naming the project and M's contact email (stored as a repository variable). Retry once with backoff on 429 or 5xx responses.

## 6. Data

All data lives in the repo as append-only Parquet files, partitioned by month.

**Sailing log** (`data/sailings/`): one row each time a sailing's observed state changes.
- `sailing_id` (route code + direction + scheduled departure in UTC), `route`, `from_port`, `to_port`, `scheduled_departure`, `vessel`, `status`, `reason`, `notice_text`, `first_seen_at`, `observed_at`, `source` (`live` or the FOI reference).
- The label for a sailing is its last observed state at scheduled departure plus two hours.

**Outcome classes:** `operated`, `diverted` (for example, Ardrossan to Troon), `cancelled_weather`, `cancelled_other`. The headline figure is the probability of `operated`; diversion probability is shown separately.

**Forecast log** (`data/forecasts/`): one row per model, issue time, point and valid hour.
- Wind speed and gusts at 10 m, wind direction, wave height, wave period, lead time in hours.
- Models: ECMWF IFS (backfillable). A high-resolution UK model is added for the first 48 hours only if Open-Meteo carries one; this is checked at build time and the design does not depend on it.

**Timetable** (`data/timetable/`): next five days of sailings with vessel assignment and each vessel's ordered rotation, including overnight berth.

**Backfill** (`data/backfill/`): CalMac FOI files (4248, 4219, 4202, 4256) normalised to the sailing-log schema and tagged with their FOI reference; Open-Meteo Single Runs data from March 2024 at the matching lead times.

**Adjustments** (`adjustments.json`): `scope` (sailing, vessel, route or date range), `type` (`override` or `multiply`), `value`, `reason`, `source_url`, `created_at`, `expires_at`. Every adjustment must cite a source; expired entries are ignored.

## 7. Prediction model

**Layer 1: weather risk per crossing.** Features for each sailing's crossing window: mean wind, maximum gust, direction relative to each port's exposure, wave height, lead time, port, vessel, time of day and month.

**Layer 2: rotation and knock-on.** For each vessel, a Monte Carlo simulation (1,000 runs) of the day's sailings in order, tracking the vessel's port. A failed sailing strands the vessel and fails dependent sailings, including across midnight. A learned pre-emptive cancellation factor captures CalMac's habit of cancelling an evening sailing ahead of an adverse overnight forecast.

**Layer 3: non-weather risk.** A rolling per-vessel base rate of technical and operational cancellations. Known current issues arrive through Claude adjustments.

**Layer 4: diversion.** For Ardrossan sailings, the probability of diverting to Troon, learned from logged diversions.

**Known cancellations** announced by CalMac override the model to 0% as soon as `collect` sees them.

**Model progression**
1. **Week one:** rule-based baseline from CalMac's wind thresholds (FOI 4219 if received; otherwise estimated from logged outcomes and route averages).
2. **After backfill:** calibrated logistic model.
3. **Only if it beats the logistic model on an unseen winter:** monotonic gradient boosting.
4. **New vessels** (for example, Glen Rosa) start from the route average and are flagged low confidence until they have their own history.

**Display by lead time** (calendar days, UK time)
- Today and the next two days: percentage.
- The two days after that: band. Likely is 75% or more; uncertain is 40% to 74%; unlikely is below 40%.
- For scoring, lead time is measured in hours from forecast issue and bucketed into 24-hour lead days (day one is 0 to 24 hours).

**Each prediction carries:** probability operated, probability diverted, band, main driver (for example, "SW gusts 45 kt at Ardrossan"), raw and adjusted values, and a low-confidence flag where relevant.

## 8. Outputs

**Phone page (GitHub Pages)**
- Top: today's outlook from `outlook.md`.
- Main: a five-day timeline per route; each sailing is a coloured chip (green, amber, red) showing a percentage or band. Tapping shows the driver, diversion risk and any adjustment.
- Footer: recent accuracy, last updated time, a stale warning, Open-Meteo attribution, and "Unofficial. Always check CalMac before travelling."
- Until launch: a "beta" banner and `noindex`.

**Home Assistant:** a REST sensor on `predictions.json` (15-minute interval), template sensors for the next sailings per route, a dashboard card, and a staleness sensor on `health.json`. YAML is supplied by this project and installed through the home automation project.

**Sharing:** share the page URL. No rebuild needed.

## 9. Error handling

| Failure | Behaviour |
|---|---|
| CalMac feed down or changed shape | Response validated against a schema; one retry; nothing written on failure. Three consecutive failures open a GitHub issue (GitHub emails M). |
| Open-Meteo unavailable | Keep the last forecast; mark predictions stale; page warns after six hours. |
| Collector stale | Page warns if no successful collect for one hour. |
| Any job fails mid-run | Outputs are swapped in only when complete; the last good version stays live. |
| Push conflict between jobs | Rebase and retry up to three times; then fail the run (GitHub emails M). |
| Claude review late or missing | Raw predictions shown; expired adjustments ignored. |
| Scheduler delay | No job depends on exact timing. |
| Monitoring | `health.json` records each job's last success; Home Assistant can flag staleness; GitHub emails on workflow failure. |

## 10. Testing and success criteria

**Tests**
- Unit tests on saved real feed snapshots, including the 23 November 2026 Troon notice: cancelling the 18:10 from Troon must cancel the 07:30 from Brodick the next morning.
- Unit tests for the timetable parser, feature builder and rotation simulator.
- Time-ordered backtest: train on the past, test on an unseen winter, using forecasts exactly as issued at each lead time (no look-ahead).

**Benchmarks to beat**
1. Climatology: the route's monthly cancellation rate.
2. A plain wind-threshold rule, roughly what an XC Weather user does by eye.

**Live scoring:** every sailing is scored after departure, by lead day, for raw and adjusted predictions separately. Metrics: Brier score, calibration by probability band, missed disruptions and false alarms, and per-vessel accuracy.

**Launch bar for sharing with islanders:** over one full winter (November to March) of live predictions, beat both benchmarks on Brier score at lead days one to three, and stay within 10 percentage points of observed rates in every calibration band.

## 11. Build phases

| Phase | Scope | Outcome |
|---|---|---|
| 0. Prerequisites | Send the FOI email; obtain the CalMac feed endpoint and parser from the home automation project; create the public repo | Inputs in hand |
| 1. Start the clock | `collect`, `forecast`, `health.json`, failure issues | Live logging running |
| 2. First useful output | `timetable`, rotation simulator, rule baseline, `predict`, `publish`, HA YAML | Page live in beta |
| 3. Learn from history | Backfill ingestion, calibrated model, backtest, `train`, `accuracy.json` | Data-driven predictions |
| 4. Judgement layer | Claude daily review, adjustments, separate scoring | Non-weather causes covered |
| 5. Prove it | One winter of live scoring against the launch bar | Decision on sharing |

## 12. Dependencies

| Dependency | Needed for | Fallback if unavailable |
|---|---|---|
| CalMac feed endpoint and parser (home automation project) | Phase 1 | Reverse-engineer the endpoint behind calmac.co.uk/en-gb/service-status |
| CalMac FOI responses | Phase 3 | Train on live data only; the baseline rule carries predictions meanwhile |
| Future timetable in the CalMac feed | Phase 2 | Parse the published timetable PDFs |
| High-resolution UK model on Open-Meteo | Optional | ECMWF only |
| GitHub Actions and Pages (free tier, public repo) | All phases | GitHub Pro for a private repo |
| Claude scheduled task with push access to the repo | Phase 4 | Claude review writes to a pull request that M merges |

## 13. Later candidates (not in version one)

- AIS verification of sailings (aisstream.io).
- Ensemble forecast spread to widen uncertainty at longer lead times.
- Tide as a feature (Brodick has had tide-related disruption).
- Push alerts for planned sailings.
- Claonaig to Lochranza as the fallback route.

## 14. Decisions log

| Decision | Choice | Reason |
|---|---|---|
| Granularity | Per sailing | Wind varies within a day; cancellations knock on across days |
| Outputs | Phone page plus Home Assistant | Often away from the HTPC; dashboard not reachable remotely |
| Approach | Python pipeline on GitHub Actions plus daily Claude review | Deterministic, free, testable; Claude covers messy non-weather causes |
| Rejected | Claude-only; Home Assistant-hosted | Non-deterministic and usage-heavy; reintroduces remote-access dependency |
| History | Request copies of released FOI responses | Data already compiled by CalMac |
| Hosting | Public repo, GitHub Pages | Free; no personal data; easy to share later |
| Days 4 and 5 | Bands, not percentages | Hourly gust forecasts lose skill at that range |
| Sharing | Gated on a measurable launch bar | Avoid islanders relying on an unproven tool |
