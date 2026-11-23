import pytest

from ferrypredictor import __main__ as cli


@pytest.fixture(autouse=True)
def isolated_dirs(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(cli, "SITE_DIR", tmp_path / "site")


@pytest.fixture
def calls(monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "_collect", lambda env, now: seen.append(("collect", env)) or {"ok": 1})
    monkeypatch.setattr(cli, "_ais", lambda env, now: seen.append(("ais", env)) or {"ok": 1})
    monkeypatch.setattr(cli, "_forecast", lambda env, now: seen.append(("forecast", env)) or {"ok": 1})
    return seen


def test_dispatches_collect(calls, monkeypatch):
    monkeypatch.setenv("CONTACT_EMAIL", "a@b.c")
    assert cli.main(["collect"]) == 0
    assert calls[0][0] == "collect"


def test_missing_contact_email_exits_2(calls, monkeypatch, capsys):
    monkeypatch.delenv("CONTACT_EMAIL", raising=False)
    assert cli.main(["collect"]) == 2
    assert "CONTACT_EMAIL" in capsys.readouterr().err
    assert calls == []


def test_ais_needs_api_key_and_never_prints_it(calls, monkeypatch, capsys):
    monkeypatch.setenv("CONTACT_EMAIL", "a@b.c")
    monkeypatch.delenv("AISSTREAM_API_KEY", raising=False)
    assert cli.main(["ais"]) == 2
    monkeypatch.setenv("AISSTREAM_API_KEY", "secret-value")
    assert cli.main(["ais"]) == 0
    out = capsys.readouterr()
    assert "secret-value" not in out.out + out.err


def test_job_failure_exits_1_and_records_health(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("CONTACT_EMAIL", "a@b.c")

    def boom(env, now):
        raise RuntimeError("HTTP 503 from CalMac")

    monkeypatch.setattr(cli, "_collect", boom)
    assert cli.main(["collect"]) == 1
    assert "HTTP 503" in capsys.readouterr().err
    assert "HTTP 503" in (tmp_path / "site" / "health.json").read_text()


def test_unknown_job_is_rejected():
    with pytest.raises(SystemExit):
        cli.main(["nope"])
