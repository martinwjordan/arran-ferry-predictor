from pathlib import Path

import pytest
import yaml

WF = Path(__file__).resolve().parents[1] / ".github" / "workflows"
SCHEDULES = {
    "collect": "0,20,40 * * * *",
    "ais": "10,30,50 * * * *",
    "forecast": "50 0,6,12,18 * * *",
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
    assert wf["permissions"] == {"contents": "write", "issues": "write", "actions": "read"}


@pytest.mark.parametrize("name", SCHEDULES)
def test_job_runs_the_right_command_and_commits_to_data_branch(name):
    steps = load(name)["jobs"][name]["steps"]
    runs = [s.get("run", "") for s in steps]
    assert f"python -m ferrypredictor {name}" in runs
    assert "bash scripts/checkout_data_branch.sh store" in runs
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
