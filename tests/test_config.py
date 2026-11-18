from datetime import time

from app.config import load_settings


def test_quiet_hours_parsed(monkeypatch):
    monkeypatch.setenv("QUIET_HOURS", "22:00-07:00")
    assert load_settings().quiet_hours == (time(22), time(7))


def test_quiet_hours_default(monkeypatch):
    monkeypatch.delenv("QUIET_HOURS", raising=False)
    assert load_settings().quiet_hours == (time(22), time(7))
