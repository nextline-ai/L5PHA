from contextlib import closing

import pytest

from veyquant.model_prompts import DEFAULT_PROMPTS, harness, prompt_catalog, prompt_selection
from veyquant.operating_policy import OperatingPolicy, PolicyConflict
from veyquant.shadow_inference import model_system
from veyquant.store import Store

LIMITS = {"capital_krw": "1000000", "max_order_krw": "100000", "max_daily_loss_krw": "50000"}


def test_migration_restart_and_partial_policy_update_preserve_role_prompts(tmp_path):
    path = str(tmp_path / "policy.db")
    custom = {role: f"custom-{role}" for role in DEFAULT_PROMPTS}
    with closing(Store(path)) as store:
        policy = OperatingPolicy(store)
        store.db.execute("ALTER TABLE operating_policy DROP COLUMN prompts")
        policy = OperatingPolicy(store)
        assert policy.view()["prompts"] == DEFAULT_PROMPTS
        assert policy.view()["revision"] == 0
        policy.save(LIMITS, "0", 1000, prompts=custom)
        with pytest.raises(PolicyConflict):
            policy.save(LIMITS, "0", 1001, prompts=DEFAULT_PROMPTS)
        assert policy.save(LIMITS, "1", 1002)["prompts"] == custom
    with closing(Store(path)) as store:
        assert OperatingPolicy(store).view()["prompts"] == custom


@pytest.mark.parametrize("bad", ["", " ", "x" * 2001, "x\x00", 42])
def test_invalid_prompt_is_rejected_before_save(bad):
    with pytest.raises(ValueError, match="invalid_role_prompt"):
        prompt_selection(DEFAULT_PROMPTS | {"cheap": bad})


def test_harness_cannot_be_overridden_and_other_roles_do_not_leak():
    prompts = {role: f"PRIVATE-{role}" for role in DEFAULT_PROMPTS}
    catalog = prompt_catalog()
    for role in DEFAULT_PROMPTS:
        text = model_system(role, {"protocol": "decision-v2"}, prompts=prompts)
        assert text.startswith(catalog["harnesses"][role])
        assert catalog["harnesses"][role] == harness(role)
        assert prompts[role] in text
        assert all(prompts[other] not in text for other in prompts if other != role)
    with pytest.raises(ValueError, match="invalid_role_prompts"):
        prompt_selection(prompts | {"harness": "execute broker orders"})


def test_research_harness_explains_two_intraday_slots_and_holidays():
    from veyquant.model_prompts import harness

    text = harness("research")
    for required in (
        "주말을 제외",
        "10:30·14:00(KST)",
        "장중 2회",
        "휴장일",
        "시작 90분 후",
        "마감 90분 전",
        "횟수 상한이나 쿨다운이 없습니다",
    ):
        assert required in text
