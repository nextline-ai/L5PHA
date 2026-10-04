import copy

import pytest
from test_decision_pipeline import CALENDAR, NOW
from test_decision_pipeline import store as store

from veyquant.decision_runtime import corroborated_signals, surveillance_summary
from veyquant.surveillance import observe


def sample(store, at, *, price=0, spread=10, z=0):
    metric = {
        "symbol": "000001",
        "as_of": at,
        "price_as_of": at,
        "volume_as_of": at,
        "book_as_of": at,
        "change_5m": price,
        "volume_change_5m": price,
        "volume_zscore": z,
        "spread_bp": spread,
    }
    observe(store, {"last_realtime_at": at, "metrics": [metric]}, CALENDAR, at)


def ordinary(store, now):
    return [s for s in store.take_signals(now) if s["condition"] != "heartbeat"]


def test_old_low_thresholds_do_not_generate_noise(store):
    sample(store, NOW, price=0.031, spread=70, z=4.5)
    assert ordinary(store, NOW + 300) == []


def test_spread_hysteresis_does_not_rearm_at_entry_boundary(store):
    sample(store, NOW, spread=105)
    assert [s["condition"] for s in ordinary(store, NOW + 300)] == ["spread_100bp"]
    for i, value in enumerate([99, 101, 80, 120]):
        sample(store, NOW + 305 + i * 5, spread=value)
    assert ordinary(store, NOW + 700) == []
    sample(store, NOW + 701, spread=59)
    sample(store, NOW + 702, spread=100)
    assert len(ordinary(store, NOW + 1002)) == 1


def test_normalization_cancels_old_batch_before_new_episode(store):
    sample(store, NOW, price=0.051)
    sample(store, NOW + 10, price=0.01)
    sample(store, NOW + 20, price=0.051)
    assert ordinary(store, NOW + 300) == []
    assert [s["condition"] for s in ordinary(store, NOW + 320)] == ["price_5pct"]


def test_shock_is_immediate_and_supersedes_same_stock_batched_noise(store):
    sample(store, NOW, price=0.051, spread=110)
    assert ordinary(store, NOW) == []
    sample(store, NOW + 5, price=0.08, spread=110)
    signals = ordinary(store, NOW + 5)
    assert {s["condition"] for s in signals} == {"price_shock", "price_5pct", "spread_100bp"}
    assert ordinary(store, NOW + 305) == []


def test_new_shocks_have_no_daily_quota_or_cooldown(store):
    for n in range(12):
        at = NOW + n * 10
        sample(store, at, price=0.01)
        sample(store, at + 1, price=-0.09)
        assert any(s["condition"] == "price_shock" for s in ordinary(store, at + 1))


@pytest.mark.parametrize("movement", [0.02, -0.02])
def test_volume_confirmation_applies_to_both_directions(store, movement):
    sample(store, NOW, price=movement, z=6)
    assert [s["condition"] for s in ordinary(store, NOW + 300)] == ["price_volume"]
    sample(store, NOW + 301, price=movement, z=5)
    sample(store, NOW + 302, price=movement, z=7)
    assert ordinary(store, NOW + 602) == []
    sample(store, NOW + 603, price=movement, z=2)
    sample(store, NOW + 604, price=movement, z=7)
    assert len(ordinary(store, NOW + 904)) == 1


def test_immediate_disclosure_is_not_cancelled_by_normalization(store):
    store.edge("filing", True, NOW, {"kind": "dart_important"}, immediate=True)
    store.edge("filing", False, NOW + 1, {})
    assert store.take_signals(NOW + 1)[0]["kind"] == "dart_important"


def test_cross_stock_or_cross_time_maxima_are_not_corroboration():
    first = {
        "condition": "price_5pct",
        "symbol": "000001",
        "metrics": {"change_5m": 0.07, "price_as_of": NOW, "spread_bp": 10, "book_as_of": NOW},
    }
    second = {
        "condition": "spread_100bp",
        "symbol": "000002",
        "metrics": {"change_5m": 0.001, "price_as_of": NOW, "spread_bp": 300, "book_as_of": NOW},
    }
    assert corroborated_signals([first, second])["count"] == 0
    aligned = copy.deepcopy(first)
    aligned["metrics"]["spread_bp"] = 300
    assert corroborated_signals([aligned])["count"] == 1
    aligned["metrics"]["book_as_of"] = NOW - 31
    assert corroborated_signals([aligned])["count"] == 0
    summary = surveillance_summary([first, first, second])
    assert summary["signal_count"] == 3 and summary["distinct_symbols"] == 2
    assert summary["market_breadth"].startswith("unknown")
    assert summary["corroboration"]["count"] == 0


def test_missing_volume_observation_time_cannot_confirm_joint_evidence():
    s = {"symbol": "000001", "metrics": {"volume_change_5m": 0.05, "volume_zscore": 100}}
    assert corroborated_signals([s])["count"] == 0
    s["metrics"]["volume_as_of"] = NOW
    assert corroborated_signals([s])["count"] == 1


def test_opposite_price_shock_is_new_material_evidence_without_waiting(store):
    sample(store, NOW, price=0.09)
    assert any(s["condition"] == "price_shock" for s in ordinary(store, NOW))
    sample(store, NOW + 1, price=-0.09)
    assert any(s["condition"] == "price_shock" for s in ordinary(store, NOW + 1))
