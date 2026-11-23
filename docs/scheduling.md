# Scheduling

The jobs have GitHub cron schedules, but GitHub had not fired a single scheduled run three hours after the workflows went live on 23 November 2026, even after the schedules were moved off busy minutes and the workflows were switched off and on again. Manual runs work.

So the jobs are started from outside, through GitHub's API, by a free timer service. The GitHub schedules stay in place: if both fire, the concurrency groups queue the second run, and a repeat run writes nothing new.

## Live setup

Set up on 23 November 2026, on cron-job.org (account: martinwjordan@gmail.com).

| Job | Schedule (UTC) | Alert email after | First timer run |
|---|---|---|---|
| collect | `7,27,47 * * * *` | 3 failures in a row | 16:47, 23 Nov 2026 |
| forecast | `53 */6 * * *` | 2 failures in a row | 18:53, 23 Nov 2026 (test run 18:09 succeeded) |

- **Token:** fine-grained, `arran-ferry-predictor` only, Actions read and write. **Expires 22 November 2027.** Both timers stop on that date: generate a new token and paste it into each job's Authorization header before then.
- cron-job.org also emails on recovery and if it disables a job for repeated failures.

## 1. Create a token (GitHub, signed in as martinwjordan)

Settings > Developer settings > Personal access tokens > Fine-grained tokens > Generate new token.

- **Repository access:** Only select repositories > `arran-ferry-predictor`.
- **Permissions:** Actions: Read and write. Metadata: Read-only is added automatically.
- **Expiry:** the longest allowed. Note the date; the jobs stop when it expires.

Copy the token once; GitHub won't show it again.

## 2. Create the timers (cron-job.org, free account)

Set the account time zone to UTC. Create two cron jobs:

| Job | URL | Schedule (UTC) |
|---|---|---|
| collect | `https://api.github.com/repos/martinwjordan/arran-ferry-predictor/actions/workflows/collect.yml/dispatches` | minutes 7, 27, 47 of every hour |
| forecast | `https://api.github.com/repos/martinwjordan/arran-ferry-predictor/actions/workflows/forecast.yml/dispatches` | 00:53, 06:53, 12:53, 18:53 |

For both, under Advanced:

- **Request method:** POST
- **Headers:**
  - `Accept: application/vnd.github+json`
  - `Authorization: Bearer <your token>`
  - `X-GitHub-Api-Version: 2022-11-28`
  - `Content-Type: application/json`
- **Request body:** `{"ref":"main"}`

A successful call returns HTTP 204. Use cron-job.org's "Test run" and check that a new run appears in the repo's Actions tab.

## Checking it works

- The Actions tab shows `collect` runs every 20 minutes, triggered by `workflow_dispatch`.
- `site/health/collect.json` on the `data` branch updates every run.
- Three failures in a row still open a `pipeline-failure` issue.
