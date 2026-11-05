"""Trade rule: YES on the highest-probability live bucket, NO on every other live bucket.
Kalshi's prices are NOT compared with the model; they are only the fill price."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from kalshi_api import algorithm as alg  # noqa: E402


def _bucket(ticker, floor, cap, yes_ask, no_ask, dead=False):
    return {
        "ticker": ticker, "label": ticker, "floor": floor, "cap": cap,
        "yes_ask_cents": yes_ask, "no_ask_cents": no_ask, "is_dead": dead, "model_prob": None,
    }


def _run(monkeypatch, buckets, pdf):
    """pdf: {temp: prob}; the model results are a single point-mass-per-temp 3h model."""
    temps = list(pdf)
    model = {"temps": temps, "probs": list(pdf.values()), "high_temps": temps, "high_probs": list(pdf.values())}
    monkeypatch.setattr(alg, "get_temp_snapshot", lambda: {"current_f": None, "daily_high_f": None})
    return alg.get_recommendation(buckets, results={"accuweather_3h": model})


# Model: 50% on 88, 30% on 90, 20% on 92 -> buckets [87,89) 50%, [89,91) 30%, [91,93) 20%
PDF = {88: 0.5, 90: 0.3, 92: 0.2}


def test_yes_on_top_bucket_and_no_on_all_others(monkeypatch):
    buckets = [
        _bucket("A", 87, 89, yes_ask=40, no_ask=61),
        _bucket("B", 89, 91, yes_ask=45, no_ask=56),
        _bucket("C", 91, 93, yes_ask=10, no_ask=91),
    ]
    rec = _run(monkeypatch, buckets, PDF)
    assert rec["status"] == "ready"
    assert rec["yes_trade"]["ticker"] == "A" and rec["yes_trade"]["price_cents"] == 40
    assert rec["yes_trade"]["model_prob"] == pytest.approx(0.5)
    assert [t["ticker"] for t in rec["no_trades"]] == ["B", "C"]
    assert [t["price_cents"] for t in rec["no_trades"]] == [56, 91]


def test_trades_even_when_kalshi_is_above_or_below_the_model(monkeypatch):
    # Old rule skipped these: A has model 50% < Kalshi 70% (no YES), C has model 20% > Kalshi 9% (no NO).
    buckets = [
        _bucket("A", 87, 89, yes_ask=70, no_ask=31),
        _bucket("B", 89, 91, yes_ask=20, no_ask=81),
        _bucket("C", 91, 93, yes_ask=10, no_ask=91),
    ]
    rec = _run(monkeypatch, buckets, PDF)
    assert rec["status"] == "ready"
    assert rec["yes_trade"]["ticker"] == "A"
    assert [t["ticker"] for t in rec["no_trades"]] == ["B", "C"]


def test_dead_buckets_are_never_traded_and_top_is_picked_among_live_ones(monkeypatch):
    buckets = [
        _bucket("A", 87, 89, yes_ask=70, no_ask=31),
        _bucket("D", 85, 87, yes_ask=2, no_ask=99, dead=True),
        _bucket("B", 89, 91, yes_ask=45, no_ask=56),
    ]
    rec = _run(monkeypatch, buckets, PDF)
    tickers = [rec["yes_trade"]["ticker"]] + [t["ticker"] for t in rec["no_trades"]]
    assert tickers == ["A", "B"]                      # D (dead) is absent
    assert "A" not in [t["ticker"] for t in rec["no_trades"]]   # top bucket: YES only


def test_prices_without_upside_or_without_an_ask_are_skipped(monkeypatch):
    buckets = [
        _bucket("A", 87, 89, yes_ask=99, no_ask=2),     # YES ask 99 > cap -> no YES
        _bucket("B", 89, 91, yes_ask=1, no_ask=99),     # NO ask 99 > cap  -> skip
        _bucket("C", 91, 93, yes_ask=5, no_ask=None),   # no NO ask        -> skip
    ]
    rec = _run(monkeypatch, buckets, {88: 0.995, 90: 0.003, 92: 0.002})
    assert rec["yes_trade"] is None
    assert rec["no_trades"] == []
    assert rec["status"] == "waiting"


def test_waits_when_every_live_bucket_is_dead(monkeypatch):
    rec = _run(monkeypatch, [_bucket("A", 87, 89, yes_ask=2, no_ask=99, dead=True)], PDF)
    assert rec["status"] == "waiting" and rec["yes_trade"] is None and rec["no_trades"] == []
