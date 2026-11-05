from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from engine.config import load_policy
from engine.engine import PolicyEngine, check_standard_window
from engine.models import CustomerClaim, EngineRequest, OrderFact, Variant

REAL_CONFIG = Path(__file__).parent.parent / "policy" / "rules.yaml"

# March: well clear of the holiday window (Nov 15 to Dec 31)
ORDERED = date(2026, 2, 27)
DELIVERED = date(2026, 3, 2)


@pytest.fixture(scope="module")
def config():
    return load_policy(REAL_CONFIG)


@pytest.fixture(scope="module")
def engine(config):
    return PolicyEngine(config)


def make_request(days_after_delivery: int) -> EngineRequest:
    """A plain request: not a member, not a gift, not final sale, not defective, unused."""
    return EngineRequest(
        order_fact=OrderFact(
            order_id="A1001",
            order_date=ORDERED,
            delivery_date=DELIVERED,
            loyalty_since=None,
            item="Linen shirt",
            price_paid=Decimal("40.00"),
            is_final_sale=False,
            is_gift=False,
            purchased_variant=Variant(product_id="P100", size="M", color="blue"),
        ),
        customer_claim=CustomerClaim(defective=False, unused_with_tags_or_packaging=True),
        request_date=DELIVERED + timedelta(days=days_after_delivery),
    )


# -- C11: day 30 is inside the window, day 31 is not
@pytest.mark.parametrize("days", [0, 30], ids=["delivery day", "day 30"])
def test_within_standard_window_refunds(engine, days):
    decision = engine.evaluate(make_request(days))

    assert decision.action_to_take == "refund"
    assert decision.refund_amount == Decimal("40.00")
    assert decision.deadline == DELIVERED + timedelta(days=30)
    assert [(h.rule_id, h.result) for h in decision.policy_rules] == [("standard_window", "pass")]


def test_day_31_denied(engine):
    decision = engine.evaluate(make_request(31))

    assert decision.action_to_take == "deny"
    assert decision.refund_amount is None
    assert decision.deadline == DELIVERED + timedelta(days=30)
    assert [(h.rule_id, h.result) for h in decision.policy_rules] == [("standard_window", "fail")]


def test_trace_explains_the_decision(engine):
    (hit,) = engine.evaluate(make_request(31)).policy_rules
    assert hit.section == "S1"
    assert hit.detail == "Requested Apr 2, 2026, after the deadline of Apr 1, 2026."


def test_decision_records_policy_version(engine, config):
    assert engine.evaluate(make_request(5)).rules_version == config.policy_version


def test_window_length_comes_from_config(config):
    """Change the window in config, not code: a 45-day window accepts day 45."""
    longer = config.rules.standard_window.model_copy(update={"days": 45})
    result = check_standard_window(longer, make_request(45))

    assert result.deadline == DELIVERED + timedelta(days=45)
    assert result.hits[0].result == "pass"
