"""
The DSM high raises today's high only when it is HIGHER than the HFMETAR high; otherwise HFMETAR is used unchanged.
Also: the trading window starts at 12:30 PM ET.
"""
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from kalshi_api import nws  # noqa: E402

# 2026-11-05 18:00Z = 1:00 PM EST: LST and Eastern dates agree, so a DSM for "today" is usable.
NOW = datetime(2026, 11, 5, 18, 0, tzinfo=timezone.utc)
TODAY = "2026-11-05"


def dsm(high=85, date=TODAY, kind="partial", report="15:00"):
    entry = {"kind": kind, "date": date, "high_f": high, "issued_at": "2026-11-05T20:11:00+00:00",
             "report_time_lst": report, "product_id": "P"}
    return {"latest": entry, "last_final": None, "stale": False}


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(nws, "_floor_fail_until", 0.0)
    nws._cache.clear()
    yield
    nws._cache.clear()


def use_dsm(monkeypatch, payload):
    monkeypatch.setattr(nws, "get_dsm", lambda: payload)


# ── the rule ──────────────────────────────────────────────────────────────────

def test_a_higher_dsm_high_replaces_the_hfmetar_high(monkeypatch):
    use_dsm(monkeypatch, dsm(high=85))
    assert nws.apply_dsm_floor(84.2, NOW) == 85.0           # whole-degree-C feed understated the true 85F
    decision = nws.last_floor_decision()
    assert decision["used"] is True and decision["dsm_high_f"] == 85 and decision["effective_high_f"] == 85.0


@pytest.mark.parametrize("hf,dsm_high", [(87.8, 87), (84.2, 84), (84.2, 80), (90.0, 90)])
def test_a_dsm_that_is_not_higher_is_not_used(monkeypatch, hf, dsm_high):
    use_dsm(monkeypatch, dsm(high=dsm_high))
    assert nws.apply_dsm_floor(hf, NOW) == hf                # HFMETAR value returned exactly as given
    assert nws.last_floor_decision()["used"] is False
    assert "not higher" in nws.last_floor_decision()["reason"]


def test_an_implausibly_high_dsm_is_rejected(monkeypatch):
    use_dsm(monkeypatch, dsm(high=90))
    assert nws.apply_dsm_floor(80.6, NOW) == 80.6
    assert "implausibly" in nws.last_floor_decision()["reason"]


def test_a_dsm_exactly_at_the_limit_is_accepted(monkeypatch):
    use_dsm(monkeypatch, dsm(high=84))
    assert nws.apply_dsm_floor(80.0, NOW) == 84.0


def test_no_hfmetar_high_means_nothing_to_raise(monkeypatch):
    use_dsm(monkeypatch, dsm(high=85))
    assert nws.apply_dsm_floor(None, NOW) is None


# ── only a DSM for today's climate day counts ─────────────────────────────────

def test_yesterdays_dsm_is_ignored(monkeypatch):
    use_dsm(monkeypatch, dsm(high=88, date="2026-11-04", kind="final", report=None))
    assert nws.apply_dsm_floor(84.2, NOW) == 84.2
    assert "not today" in nws.last_floor_decision()["reason"]


def test_in_summer_between_midnight_and_1am_edt_the_dsm_day_is_still_yesterday(monkeypatch):
    # 00:30 EDT on 15 Jul = 23:30 LST on 14 Jul: the Eastern date and the DSM (LST) date differ, so no DSM date can match.
    now = datetime(2026, 7, 15, 4, 30, tzinfo=timezone.utc)
    for date in ("2026-07-14", "2026-07-15"):
        use_dsm(monkeypatch, dsm(high=90, date=date))
        assert nws.apply_dsm_floor(88.0, now) == 88.0


def test_in_summer_after_1am_edt_todays_dsm_is_usable(monkeypatch):
    now = datetime(2026, 7, 15, 19, 0, tzinfo=timezone.utc)      # 3 PM EDT
    use_dsm(monkeypatch, dsm(high=92, date="2026-07-15"))
    assert nws.apply_dsm_floor(91.4, now) == 92.0


# ── it must never break the weather pipeline ──────────────────────────────────

@pytest.mark.parametrize("payload", [{}, {"latest": None}, {"latest": {"high_f": None, "date": TODAY}}, None])
def test_missing_or_empty_dsm_leaves_hfmetar_alone(monkeypatch, payload):
    use_dsm(monkeypatch, payload)
    assert nws.apply_dsm_floor(84.2, NOW) == 84.2
    assert nws.last_floor_decision()["used"] is False


def test_a_failing_dsm_source_falls_back_and_is_not_retried_straight_away(monkeypatch):
    calls = []

    def boom():
        calls.append(1)
        raise RuntimeError("iem down")

    monkeypatch.setattr(nws, "get_dsm", boom)
    assert nws.apply_dsm_floor(84.2, NOW) == 84.2
    assert nws.apply_dsm_floor(84.2, NOW) == 84.2
    assert len(calls) == 1                                          # backed off: the pollers are never slowed down
    assert "unavailable" in nws.last_floor_decision()["reason"]
    monkeypatch.setattr(nws, "_floor_fail_until", time.monotonic() - 1)
    use_dsm(monkeypatch, dsm(high=85))
    assert nws.apply_dsm_floor(84.2, NOW) == 85.0                   # recovers once the back-off has passed


def test_garbage_in_the_dsm_entry_cannot_raise(monkeypatch):
    use_dsm(monkeypatch, {"latest": {"high_f": "hot", "date": TODAY}})
    assert nws.apply_dsm_floor(84.2, NOW) == 84.2


# ── it is wired into both places the HFMETAR high is produced ─────────────────

def _skip_if_dates_differ():
    now = datetime.now(timezone.utc)
    if now.astimezone(nws.LST).date() != now.astimezone(nws.ET).date():
        pytest.skip("LST and Eastern dates differ right now (summer, 00:00-01:00 EDT): the DSM is correctly ignored")
    return now.astimezone(nws.LST).date().isoformat()


def test_live_monitor_uses_the_raised_high(monkeypatch):
    from kalshi_api import temp_monitor

    today = _skip_if_dates_differ()
    now = datetime.now(timezone.utc)
    observations = [{"observed_at": (now - timedelta(minutes=m)).isoformat(), "temp_f": f} for m, f in ((40, 84.2), (10, 82.4))]
    monkeypatch.setattr(temp_monitor, "fetch_recent_observations", lambda now=None, reuse_seconds=0: (observations, {}))
    use_dsm(monkeypatch, dsm(high=85, date=today))
    out = temp_monitor.fetch_current_high()
    assert out["t0_f"] == 82.4                                      # the current temperature is untouched
    assert out["today_high_f"] == 85.0                              # the high is raised to the DSM's


def test_live_monitor_keeps_hfmetar_when_dsm_is_not_higher(monkeypatch):
    from kalshi_api import temp_monitor

    today = _skip_if_dates_differ()
    now = datetime.now(timezone.utc)
    observations = [{"observed_at": (now - timedelta(minutes=10)).isoformat(), "temp_f": 87.8}]
    monkeypatch.setattr(temp_monitor, "fetch_recent_observations", lambda now=None, reuse_seconds=0: (observations, {}))
    use_dsm(monkeypatch, dsm(high=87, date=today))
    assert temp_monitor.fetch_current_high()["today_high_f"] == 87.8


def test_model_input_uses_the_raised_high(monkeypatch):
    import weather
    from kalshi_api import forecast

    today = _skip_if_dates_differ()
    now = datetime.now(timezone.utc)
    obs = {"observed_at": (now - timedelta(minutes=5)).isoformat(), "temp_f": 84.2, "relative_humidity": 0.7,
           "cloud_fraction": 0.2, "wind_speed_mph": 5.0, "wind_dir_deg": 90.0, "pressure_hpa": 1015.0}
    shared = {"metadata": {"status": "healthy", "source": "TEST"}, "current": obs, "recent_observations": [obs],
              "today": {"high_f": 84.2}, "features": {"pressure_trend_hpa_per_hr": 0.1}}
    monkeypatch.setattr(weather, "build_live_shared_input", lambda: shared)
    use_dsm(monkeypatch, dsm(high=85, date=today))
    data, _ = forecast._fetch_hfmetar_shared()
    assert data["asos_current"]["today_high_f"] == 85.0             # what every model script floors its paths at
    assert data["asos_current"]["t0_f"] == 84.2


# ── trading window: 12:30 PM to 3:30 PM ET ────────────────────────────────────

def test_trading_window_starts_at_1230_pm_et():
    from zoneinfo import ZoneInfo
    os.environ["DATABASE_PATH"] = ":memory:"
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    import django
    django.setup()
    from kalshi_api import autotrader

    et = ZoneInfo("America/New_York")
    day = datetime(2026, 11, 5, tzinfo=et)
    slots = autotrader._slots_for(day)
    assert autotrader.WINDOW_START == (12, 30)
    assert (slots[0].hour, slots[0].minute) == (12, 30) and (slots[-1].hour, slots[-1].minute) == (15, 30)
    assert len(slots) == 13
    assert autotrader.current_slot(day.replace(hour=11, minute=45)) is None       # no trade at 11:30 any more
    assert autotrader.current_slot(day.replace(hour=12, minute=31)) == slots[0]
    assert autotrader.window_open(day.replace(hour=12, minute=0)) is False
    assert autotrader.window_open(day.replace(hour=12, minute=30)) is True
