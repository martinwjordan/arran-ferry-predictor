# Arran ferry predictor: design spec

- **Date:** 23 November 2026 (revised the same day with the CalMac feed handover)
- **Owner:** Martin Jordan (M), Lamlash
- **Status:** Approved in brainstorming, awaiting written-spec review
- **Companion docs (project):** `claude/context-and-data-sources.md` (source research and fleet notes); `calmac-feed-arran-handover.md` (CalMac feed details, raw responses, quirks)

## 1. Summary

A scheduled tool that estimates, for every timetabled Arran sailing over the next five days, the chance it sails as planned from the planned port. It covers Ardrossan to Brodick and Troon to Brodick, both directions. It combines weather forecasts with CalMac's own service history, follows each vessel's rotation so cancellations carry forward, and learns from what actually sails.

It runs as a deterministic Python pipeline on GitHub Actions. A daily Claude scheduled task adds judgement on non-weather causes. Results appear on a phone-first web page (GitHub Pages) and on M's Home Assistant dashboard. If it proves accurate over a winter, M intends to share it with the island community.

## 2. Goals and non-goals

**Goals**

1. Per-sailing likelihood for every scheduled sailing up to five days ahead, for both Arran routes and both directions.
2. Capture knock-on effects across vessel rotations and across day boundaries (for example, a cancelled last sailing from Troon cancelling the first sailing from Brodick next morning).
3. Account for non-weather causes: vessel faults, port and operational issues, staffing, tides.
4. Learn over time from logged outcomes, and measure accuracy honestly against simple benchmarks.
5. Usable anywhere: no dependency on Home Assistant being reachable remotely.
6. Ready to share publicly without rework once accurate.

**Non-goals for version one**

- Push alerts or notifications (Home Assistant can add these later from its sensor).
- Other routes, including Claonaig to Lochranza.
- Booking availability or capacity.
- XC Weather integration.

## 3. Key constraints and context

- **Fleet changes often.** Caledonian Isles (Ardrossan) is due to retire in February; Glen Rosa is due on Troon to Brodick from February; Hebrides and Finlaggan are expected at Ardrossan later. Rotations are inferred from the timetable, never hard-coded.
- **Crossing times:** about 55 minutes from Ardrossan and 75 minutes from Troon.
- **CalMac feed:** CalMac's own website backend, `POST https://apim.calmac.co.uk/graphql`, no authentication. It is unofficial: the schema can change without notice and introspection is off.
- **The feed does not record outcomes on sailings.** Every sailing row stays `SCHEDULED`, even when cancelled. Cancellations, diversions and review times appear only in the Markdown text of `SAILING` notices. Outcome labels therefore come from parsing notice text (section 7).
- **The feed has no vessel per sailing.** Vessels are listed only per route, and that list can be stale (route 301 lists Alfred and Glen Sannox). The vessel actually operating appears in notice text.
- **The feed has no history.** Nothing before today is returned, so the pipeline logs its own snapshots from day one.
- **Timetable comes from the feed.** `portPairSailings` returns the loaded timetable period (on 23 November: to 25 March 2027 for route 003, to 18 January 2027 for route 301). No PDF parsing is needed.
- **Forecasts:** Open-Meteo (free, no authentication, CC BY 4.0), including archived model runs that allow backfill from March 2024 without look-ahead.

## 4. Architecture

```
Public GitHub repo: arran-ferry-predictor (M's personal account)
 ├─ collect    every 20 min          CalMac routes, notices, sailings → data/snapshots/, data/notices/
 ├─ parse      after collect         notice text → per-sailing events   → data/events/
 ├─ forecast   4× daily              Open-Meteo wind and waves          → data/forecasts/
 ├─ predict    after forecast, and   per-sailing probabilities          → site/predictions.json
 │             after any new event
 ├─ train      weekly                labels, refit and backtest         → models/, site/accuracy.json
 └─ publish    after predict         static page                        → GitHub Pages

Claude daily review (05:45 UK)  reads notices, unparsed text and news
                                → commits adjustments.json, outlook.md, label_reviews.json
Home Assistant                  reads site/predictions.json every 15 min
```

**Principles**

- Each job does one thing and talks to the others only through files in the repo.
- Claude never publishes and never edits the model. It commits adjustments and label reviews with reasons; the pipeline applies them and scores raw and adjusted predictions separately.
- Jobs write to separate paths, so they never need to block each other. Each commit rebases and retries (up to three times) on a push conflict. Runs of the same job share a concurrency group so they never overlap.
- Outputs are built in full, then swapped in. A failed run leaves the last good version live.

## 5. Components

| Component | Trigger | Input | Output |
|---|---|---|---|
| `collect` | Every 20 minutes | Three GraphQL calls (below) | New or changed notices to `data/notices/`; route status and sailing snapshots to `data/snapshots/` when changed; `health.json` |
| `parse` | After `collect` writes a new notice version | Notice Markdown | Per-sailing events to `data/events/`; unparsed sentences flagged for Claude review |
| `forecast` | Four times daily, timed to follow Open-Meteo's ECMWF updates | Open-Meteo Forecast and Marine APIs for five points | Appends a full run to `data/forecasts/` |
| `predict` | After `forecast`, and after `parse` records a new event | Latest forecast, sailings, events, vessel state, model, `adjustments.json` | `site/predictions.json` |
| `train` | Weekly, Sunday 03:00 UTC | All logs, label reviews and backfill | `models/current.*`, `site/accuracy.json`, a backtest report |
| `publish` | After `predict` | `predictions.json`, `accuracy.json`, `outlook.md` | Static site on GitHub Pages |
| Claude review | Claude scheduled task, daily 05:45 UK | Predictions, notices, unparsed sentences, CalMac customer updates, recent news | Commits `adjustments.json`, `outlook.md`, `label_reviews.json` |
| HA sensor | Every 15 minutes | Public `predictions.json` URL | Home Assistant sensors and dashboard card (YAML supplied; installed via the home automation project) |

**CalMac calls per `collect` run** (all server-side filtered, about 13 KB for status):
1. `routes (where: { routeCode: { in: ["003","301"] } })`: route status, route-level vessels, and all `routeStatuses` with full `detail`, `disruptionReason`, start, end and updated times.
2. `portPairSailings` by `route.routeCode` for `003`, today to today plus six days: id, sailing date, departure, arrival, rescheduled times, origin, destination.
3. The same for `301`.

**Forecast points:** Ardrossan 55.641N 4.822W; Troon 55.545N 4.670W; Brodick 55.577N 5.137W (port coordinates from the CalMac feed); mid-channel Ardrossan route 55.61N 4.98W; mid-channel Troon route 55.56N 4.91W.

**Polite polling:** every request carries a User-Agent naming the project and M's contact email (stored as a repository variable). Retry once with backoff on 429 or 5xx responses. About 216 calls a day to CalMac, against the Home Assistant sensor's roughly 525.

## 6. Data

All data lives in the repo as append-only Parquet files, partitioned by month.

**Notices** (`data/notices/`): one row per new version of a notice, keyed on notice `id` plus `updatedAtDateTime`. Full Markdown `detail`, `title`, `status` (`SAILING`, `SERVICE`, `INFORMATION`, `WARNING`), `subStatus`, `disruptionReason` (normalised: lower case, so `Crew Availability` and `Crew availability` match), start, end, `first_seen_at`.

**Snapshots** (`data/snapshots/`): route status and the sailing list, written only when they change. The effective route status is the worst of the route `status` and its active `SAILING` notices' `subStatus` (the route field can be milder than its own notices).

**Events** (`data/events/`): one row per sailing-level fact extracted from a notice.
- `sailing_id`, `event_type` (`cancelled`, `diverted`, `at_risk`, `at_risk_diversion`, `reinstated`, `review_at`), `diversion_port`, `reason`, `notice_id`, `notice_version`, `first_seen_at`, `parser_confidence`, matched sentence.
- Sailing IDs use CalMac's integer `portPairSailings.id`, with route code, direction and scheduled departure kept alongside for matching history.

**Vessel state** (`data/vessels/`): which vessel is operating each route, by date. Sources in priority order: Claude review, notice text ("MV Alfred is currently operating on this route"), route-level vessel list when it has a single entry.

**Forecast log** (`data/forecasts/`): one row per model, issue time, point and valid hour. Wind speed and gusts at 10 m, wind direction, wave height, wave period, lead time in hours. Models: ECMWF IFS (backfillable). A high-resolution UK model is added for the first 48 hours only if Open-Meteo carries one; the design does not depend on it.

**Backfill** (`data/backfill/`): CalMac FOI files (4248, 4219, 4202, 4256) normalised to the event schema and tagged with their FOI reference; Open-Meteo Single Runs data from March 2024 at the matching lead times.

**Adjustments** (`adjustments.json`): `scope` (sailing, vessel, route or date range), `type` (`override` or `multiply`), `value`, `reason`, `source_url`, `created_at`, `expires_at`. Every adjustment cites a source; expired entries are ignored.

**Label reviews** (`label_reviews.json`): Claude's reading of notice sentences the parser could not resolve, each with the sentence, the sailings it refers to, the outcome, and a confidence. Used for labels only, never for live predictions.

**Outcome labels.** Each sailing's label is fixed at scheduled departure plus two hours:
- `cancelled_weather` or `cancelled_other`: a `cancelled` event exists and was not later reinstated. The reason comes from the notice's `disruptionReason`.
- `diverted`: a `diverted` event exists.
- `operated`: none of the above.
- `at_risk` events are never outcomes; they are recorded as CalMac's own warnings and used as a model input and benchmark.
- Labels from the parser, Claude label reviews and FOI history are kept with their source so label quality can be audited.

## 7. Notice parsing and prediction model

**Notice parser.** Rule-based and tested against saved real notices. It splits Markdown into dated sections (for example "**Tuesday 24 November**"), finds departure times and ports ("the 18:10 departing Troon"), and classifies each sentence:
- `cancelled`: "is cancelled", "will not operate", "as a result ... is cancelled".
- `diverted`: "will operate from Troon", "diverted to Troon".
- `at_risk` and `at_risk_diversion`: "liable to disruption", "heightened risk of diversion", "liable to operate from Troon".
- `review_at`: "there will be a review at 13:15".
- Each extracted time is matched to the sailing with that departure, port and date. Sentences that mention a sailing but match no rule, or match more than one sailing, are flagged for Claude's label review. They are never guessed.

**Layer 1: weather risk per crossing.** Features for each sailing's crossing window: mean wind, maximum gust, direction relative to each port's exposure, wave height, lead time, port, vessel, time of day and month.

**Layer 2: rotation and knock-on.** Rotations are built by chaining sailings on each route: a sailing follows another when it departs from the previous arrival port after a minimum turnaround. With one vessel per route (the current winter pattern) this is a simple alternating chain; with two vessels the chaining separates them. For each chain, a Monte Carlo simulation (1,000 runs) of the sailings in order tracks where the vessel is. A failed sailing strands the vessel and fails dependent sailings, including across midnight. A learned pre-emptive cancellation factor captures CalMac's habit of cancelling an evening sailing ahead of an adverse overnight forecast.

**Layer 3: non-weather risk.** A rolling per-vessel base rate of technical, operational, crew and tidal cancellations. Known current issues arrive through Claude adjustments.

**Layer 4: diversion.** For Ardrossan sailings, the probability of diverting to Troon, learned from logged diversions.

**CalMac's own signals.** A live `cancelled` event sets that sailing to 0%. Live `at_risk` and `at_risk_diversion` events are model inputs, not overrides.

**Model progression**
1. **Week one:** rule-based baseline from CalMac's wind thresholds (FOI 4219 if received; otherwise estimated from logged outcomes and route averages).
2. **After backfill:** calibrated logistic model.
3. **Only if it beats the logistic model on an unseen winter:** monotonic gradient boosting.
4. **New vessels** (for example, Glen Rosa) start from the route average and are flagged low confidence until they have their own history.

**Display by lead time** (calendar days, UK time)
- Today and the next two days: percentage.
- The two days after that: band. Likely is 75% or more; uncertain is 40% to 74%; unlikely is below 40%.
- For scoring, lead time is measured in hours from forecast issue and bucketed into 24-hour lead days (day one is 0 to 24 hours).

**Each prediction carries:** probability operated, probability diverted, band, main driver (for example, "SW gusts 45 kt at Ardrossan" or "CalMac: heightened risk of diversion"), raw and adjusted values, and a low-confidence flag where relevant.

## 8. Outputs

**Phone page (GitHub Pages)**
- Top: today's outlook from `outlook.md`.
- Main: a five-day timeline per route; each sailing is a coloured chip (green, amber, red) showing a percentage or band. Tapping shows the driver, diversion risk, any CalMac warning and any adjustment.
- Footer: recent accuracy, last updated time, a stale warning, Open-Meteo attribution, and "Unofficial. Always check CalMac before travelling."
- Until launch: a "beta" banner and `noindex`.
- The page never calls CalMac directly (CalMac's CORS policy blocks other origins); it reads only the pipeline's own files.

**Home Assistant:** a REST sensor on `predictions.json` (15-minute interval), template sensors for the next sailings per route, a dashboard card, and a staleness sensor on `health.json`. YAML is supplied by this project and installed through the home automation project.

**Sharing:** share the page URL. No rebuild needed.

## 9. Error handling

| Failure | Behaviour |
|---|---|
| CalMac feed down | One retry with backoff; nothing written; next run tries again. |
| CalMac schema change | Responses validated against a schema; GraphQL `errors` bodies captured in full (not swallowed); three consecutive failures open a GitHub issue (GitHub emails M). |
| Notice text the parser cannot resolve | Flagged for Claude label review; never guessed. Parser coverage tracked weekly; a drop below 90% of sailing-mentioning sentences opens a GitHub issue. |
| Open-Meteo unavailable | Keep the last forecast; mark predictions stale; page warns after six hours. |
| Collector stale | Page warns if no successful collect for one hour. |
| Sailings beyond the loaded timetable | Not predicted; the page shows "timetable not yet published" for those days. |
| Any job fails mid-run | Outputs are swapped in only when complete; the last good version stays live. |
| Push conflict between jobs | Rebase and retry up to three times; then fail the run (GitHub emails M). |
| Claude review late or missing | Raw predictions shown; expired adjustments ignored; unresolved sentences wait for the next review. |
| Scheduler delay | No job depends on exact timing. |
| Timezone | Feed times carry `Z` and are treated as UTC. A test against the printed timetable runs when the first BST sailings appear (BST starts 28 March 2027). |
| Monitoring | `health.json` records each job's last success; Home Assistant can flag staleness; GitHub emails on workflow failure. |

## 10. Testing and success criteria

**Tests**
- Notice parser tests on saved real notices from the handover pack. The 23 November 2026 Troon notice must yield `cancelled` for the 18:10 Troon to Brodick on 23 November and the 07:30 Brodick to Troon on 24 November. The Ardrossan notice must yield `at_risk_diversion` for sailings from the 13:55 from Brodick on 23 November and for the 07:00 from Ardrossan on 24 November, and no `cancelled` events.
- Rotation tests: cancelling the 18:10 from Troon must fail the 07:30 from Brodick the next morning in simulation.
- Unit tests for the GraphQL client (including error bodies), schema validation, chaining, feature builder and label rules.
- Time-ordered backtest: train on the past, test on an unseen winter, using forecasts exactly as issued at each lead time (no look-ahead).

**Benchmarks to beat**
1. Climatology: the route's monthly cancellation rate.
2. A plain wind-threshold rule, roughly what an XC Weather user does by eye.
3. CalMac's own `at_risk` warnings, where issued.

**Live scoring:** every sailing is scored after departure, by lead day, for raw and adjusted predictions separately. Metrics: Brier score, calibration by probability band, missed disruptions and false alarms, and per-vessel accuracy. Label quality is audited monthly by sampling parser labels against notices.

**Launch bar for sharing with islanders:** over one full winter (November to March) of live predictions, beat the climatology and wind-rule benchmarks on Brier score at lead days one to three, and stay within 10 percentage points of observed rates in every calibration band.

## 11. Build phases

| Phase | Scope | Outcome |
|---|---|---|
| 0. Prerequisites | FOI email (sent 23 November 2026); CalMac feed details (received); create the public repo | Inputs in hand |
| 1. Start the clock | `collect` (notices, snapshots, sailings), `forecast`, `health.json`, failure issues | Live logging running |
| 2. First useful output | Notice parser, events, vessel state, chaining and simulator, rule baseline, `predict`, `publish`, HA YAML | Page live in beta |
| 3. Learn from history | Backfill ingestion, labels, calibrated model, backtest, `train`, `accuracy.json` | Data-driven predictions |
| 4. Judgement layer | Claude daily review: adjustments, outlook, label reviews; separate scoring | Non-weather causes covered |
| 5. Prove it | One winter of live scoring against the launch bar | Decision on sharing |

## 12. Dependencies

| Dependency | Needed for | Fallback if unavailable |
|---|---|---|
| CalMac GraphQL feed (unofficial) | All phases | If the schema changes: repair from CalMac's front-end queries. If withdrawn: CalMac status web pages |
| CalMac FOI responses (due by about 21 December 2026) | Phase 3 | Train on live data only; the baseline rule carries predictions meanwhile |
| High-resolution UK model on Open-Meteo | Optional | ECMWF only |
| GitHub Actions and Pages (free tier, public repo) | All phases | GitHub Pro for a private repo |
| Claude scheduled task with push access to the repo | Phase 4 | Claude review writes to a pull request that M merges |

## 13. Later candidates (not in version one)

- AIS cross-check of whether sailings physically ran (aisstream.io, free key). Proposed for Phase 1 given label risk from text parsing; pending M's decision.
- Ensemble forecast spread to widen uncertainty at longer lead times.
- Tide as a feature (`Tidal` is one of CalMac's disruption reasons).
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
| Outcome labels | Parsed from notice text; unresolved text goes to Claude review | The feed never marks sailings cancelled |
| Vessel per sailing | Inferred per route per day from notices and Claude review | The feed lists vessels only per route |
| Timetable | From the feed's `portPairSailings` | Covers the loaded timetable period; no PDF parsing |
