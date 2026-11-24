from pathlib import Path

import pytest
import yaml

WF = Path(__file__).resolve().parents[1] / ".github" / "workflows"
SCHEDULES = {
    "collect": "7,27,47 * * * *",
    "ais": "17,37,57 * * * *",
    "forecast": "53 0,6,12,18 * * *",
}


def load(name):
    data = yaml.safe_load((WF / f"{name}.yml").read_text())
    data["on"] = data.get("on", data.get(True))  # PyYAML reads the key `on` as True
    return data


@pytest.mark.parametrize("name", SCHEDULES)
def test_schedule_and_manual_trigger(name):
    wf = load(name)
    assert wf["on"]["schedule"] == [{"cron": SCHEDULES[name]}]
    assert "workflow_dispatch" in wf["on"]


@pytest.mark.parametrize("name", SCHEDULES)
def test_runs_never_overlap_and_permissions_are_minimal(name):
    wf = load(name)
    assert wf["concurrency"] == {"group": name, "cancel-in-progress": False}
    actions = "write" if name == "collect" else "read"  # collect starts the publish workflow
    assert wf["permissions"] == {"contents": "write", "issues": "write", "actions": actions}


@pytest.mark.parametrize("name", SCHEDULES)
def test_job_runs_the_right_command_and_commits_to_data_branch(name):
    steps = load(name)["jobs"][name]["steps"]
    runs = [s.get("run", "") for s in steps]
    assert f"python -m ferrypredictor {name}" in runs
    assert any(r.startswith("bash scripts/checkout_data_branch.sh store") for r in runs)
    assert any(r.startswith("bash scripts/commit_with_retry.sh") for r in runs)
    assert f"bash scripts/issue_on_repeated_failure.sh {name}.yml" in runs


@pytest.mark.parametrize("name", SCHEDULES)
def test_secrets_only_reach_the_steps_that_need_them(name):
    wf = load(name)
    text = (WF / f"{name}.yml").read_text()
    for step in wf["jobs"][name]["steps"]:
        env = step.get("env", {})
        if "AISSTREAM_API_KEY" in env:
            assert step["run"] == "python -m ferrypredictor ais"
    if name != "ais":
        assert "AISSTREAM_API_KEY" not in text
    assert "GITHUB_TOKEN" not in str(wf["jobs"][name].get("env", {}))


def test_schedules_are_offset_so_jobs_rarely_push_together():
    minutes = {name: set(SCHEDULES[name].split()[0].split(",")) for name in SCHEDULES}
    assert not minutes["collect"] & minutes["ais"]


def test_collect_parses_notices_then_predicts_with_what_they_read_checked_out():
    runs = [s.get("run", "") for s in load("collect")["jobs"]["collect"]["steps"]]
    checkout = next(r for r in runs if r.startswith("bash scripts/checkout_data_branch.sh store"))
    for folder in ("data/notices", "data/sailings", "data/events", "data/route_status", '"data/forecasts/$(date -u +%Y-%m)"'):
        assert folder in checkout
    assert "-1 day" in checkout  # last month's forecasts too
    assert runs.index("python -m ferrypredictor parse") == runs.index("python -m ferrypredictor collect") + 1
    assert runs.index("python -m ferrypredictor predict") == runs.index("python -m ferrypredictor parse") + 1
    assert runs.index("python -m ferrypredictor labels") == runs.index("python -m ferrypredictor predict") + 1
    assert runs.index("python -m ferrypredictor score") == runs.index("python -m ferrypredictor labels") + 1
    assert "data/predictions" in checkout and "data/labels_live" in checkout


def test_backfill_is_manual_only():
    wf = load("backfill")
    assert "schedule" not in wf["on"] and "workflow_dispatch" in wf["on"]
    runs = [s.get("run", "") for s in wf["jobs"]["backfill"]["steps"]]
    assert "python -m ferrypredictor backfill" in runs


def test_collect_starts_publish_by_dispatch():
    wf = load("collect")
    steps = wf["jobs"]["collect"]["steps"]
    runs = [s.get("run", "") for s in steps]
    assert any(r.startswith("gh workflow run publish.yml") for r in runs)
    pub = load("publish")
    assert set(pub["on"]) == {"workflow_dispatch"}
