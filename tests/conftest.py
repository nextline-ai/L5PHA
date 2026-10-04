import socket
from decimal import Decimal

import pytest

from veyquant.domain import AccountSnapshot, Proposal, Quote, RiskPolicy
from veyquant.store import Store


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Tests must not access a real network")

    monkeypatch.setattr(socket.socket, "connect", blocked)


@pytest.fixture
def store():
    s = Store(":memory:")
    yield s
    s.close()


@pytest.fixture
def proposal():
    return Proposal(
        "p1",
        "e1",
        "DEMO",
        "KRW",
        2,
        Decimal("100"),
        1000,
        1060,
        "policy-v1",
        "strategy-v1",
        ("source1",),
        "evidence",
        "counter",
        "unknown",
    )


@pytest.fixture
def account():
    return AccountSnapshot("KRW", Decimal("1000"), Decimal(0), {}, Decimal(0), {}, 1000, True)


@pytest.fixture
def quote():
    return Quote("DEMO", "KRW", Decimal("100"), 1000)


@pytest.fixture
def policy():
    return RiskPolicy(
        "policy-v1",
        "KRW",
        frozenset({"DEMO"}),
        Decimal("500"),
        Decimal("1000"),
        Decimal("500"),
        Decimal("0.05"),
    )
