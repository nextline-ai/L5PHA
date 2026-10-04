from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest

from veyquant.operating_policy import OperatingPolicy, PolicyConflict, validate_limits
from veyquant.shadow_inference import model_selection
from veyquant.store import Store

LIMITS = {"capital_krw": "1000000", "max_order_krw": "100000", "max_daily_loss_krw": "50000"}


def test_no_assumed_limits_or_live_activation_and_restart_preserves_policy(tmp_path):
    path = str(tmp_path / "policy.db")
    with closing(Store(path)) as store:
        policy = OperatingPolicy(store)
        assert policy.view()["limits"] is None
        saved = policy.save(LIMITS, "0", 1000)
        assert saved["revision"] == 1
        assert not saved["live_enabled"]
    with closing(Store(path)) as store:
        assert OperatingPolicy(store).view() == saved
        assert store.rows()[-1]["payload"]["limits"] == LIMITS


@pytest.mark.parametrize(
    "bad", ["0", "-1", "1.5", "1e6", "NaN", "100,000", " 100", "01", True, 1, "9" * 13]
)
def test_invalid_money_is_not_coerced(bad):
    with pytest.raises(ValueError):
        validate_limits(LIMITS | {"capital_krw": bad})


@pytest.mark.parametrize("field", ["max_order_krw", "max_daily_loss_krw"])
def test_limits_cannot_exceed_capital(field):
    with pytest.raises(ValueError):
        validate_limits(LIMITS | {field: "1000001"})


def test_concurrent_devices_cannot_overwrite_another_revision(tmp_path):
    path = str(tmp_path / "policy.db")
    with closing(Store(path)) as store:
        OperatingPolicy(store)

    def save(capital):
        with closing(Store(path)) as store:
            try:
                return OperatingPolicy(store).save(LIMITS | {"capital_krw": capital}, "0", 1000)
            except PolicyConflict:
                return None

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(save, ["2000000", "3000000"]))
    assert sum(x is not None for x in results) == 1
    with closing(Store(path)) as store:
        assert OperatingPolicy(store).view()["revision"] == 1
        assert len(store.rows()) == 1


def test_old_pending_preference_requires_fresh_choice_when_executor_is_introduced(tmp_path):
    with closing(Store(str(tmp_path / "legacy.db"))) as store:
        policy = OperatingPolicy(store)
        policy.save(
            {"capital_krw": "1000", "max_order_krw": "500", "max_daily_loss_krw": "100"},
            "0",
            1000,
            models=model_selection(),
            live_requested="true",
        )
        store.db.execute("ALTER TABLE operating_policy DROP COLUMN execution_consent_version")
        current = OperatingPolicy(store).view()
        assert current["live_requested"] is False
        assert current["revision"] == 2
        assert current["limits"]["capital_krw"] == "1000"
        assert OperatingPolicy(store).view()["revision"] == 2
