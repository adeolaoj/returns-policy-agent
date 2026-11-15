from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from engine.config import WindowRule, load_policy
from engine.engine import PolicyEngine, check_holiday_extension, check_standard_window
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


# Baseline request: not a member, not a gift, not final sale, not defective, unused.
BASE_ORDER = {
    "order_id": "A1001",
    "order_date": ORDERED,
    "delivery_date": DELIVERED,
    "loyalty_since": None,
    "item": "Linen shirt",
    "price_paid": Decimal("40.00"),
    "is_final_sale": False,
    "is_gift": False,
    "purchased_variant": Variant(product_id="P100", size="M", color="blue"),
}
BASE_CLAIM = {
    "defective": False,
    "unused_with_tags_or_packaging": True,
}


def make_request(*, days_after_delivery: int = 5, **overrides) -> EngineRequest:
    """The baseline request, with any field replaced by name.

    Each override goes to the model that owns that field, e.g.
    make_request(loyalty_since=date(2025, 1, 1), defective=True, days_after_delivery=45).
    request_date defaults to delivery_date + days_after_delivery; pass request_date to set it directly.
    """
    order = dict(BASE_ORDER)
    claim = dict(BASE_CLAIM)
    top = {}
    for key, value in overrides.items():
        if key in OrderFact.model_fields:
            order[key] = value
        elif key in CustomerClaim.model_fields:
            claim[key] = value
        elif key in ("request_date", "exchange_request"):
            top[key] = value
        else:
            raise TypeError(f"make_request() got an unknown field: {key!r}")

    top.setdefault("request_date", order["delivery_date"] + timedelta(days=days_after_delivery))
    return EngineRequest(
        order_fact=OrderFact(**order),
        customer_claim=CustomerClaim(**claim),
        **top,
    )


MEMBER_SINCE = date(2025, 1, 10)


def hit_for(decision, rule_id: str):
    """The single trace entry for one rule."""
    (hit,) = [h for h in decision.policy_rules if h.rule_id == rule_id]
    return hit


# -- Trace shape: every window reports, then C2 decides
def test_trace_lists_every_window_then_c2(engine):
    decision = engine.evaluate(make_request())

    assert [(h.rule_id, h.result) for h in decision.policy_rules] == [
        ("standard_window", "info"),
        ("loyalty_window", "info"),
        ("holiday_extension", "info"),
        ("defective_window", "info"),
        ("C2", "pass"),
    ]


# -- S1 + C11: day 30 is inside the window, day 31 is not
@pytest.mark.parametrize("days", [0, 30], ids=["delivery day", "day 30"])
def test_within_standard_window_refunds(engine, days):
    decision = engine.evaluate(make_request(days_after_delivery=days))

    assert decision.action_to_take == "refund"
    assert decision.refund_amount == Decimal("40.00")
    assert decision.deadline == DELIVERED + timedelta(days=30)
    assert hit_for(decision, "C2").result == "pass"


def test_day_31_denied(engine):
    decision = engine.evaluate(make_request(days_after_delivery=31))

    assert decision.action_to_take == "deny"
    assert decision.refund_amount is None
    assert decision.deadline == DELIVERED + timedelta(days=30)
    assert hit_for(decision, "C2").result == "fail"


def test_trace_explains_the_decision(engine):
    c2 = hit_for(engine.evaluate(make_request(days_after_delivery=31)), "C2")
    assert c2.section is None
    assert c2.detail == (
        "Latest deadline Apr 1, 2026, from standard_window. Requested Apr 2, 2026, after it."
    )


# -- S2 + C4: members get 60 days, if they were members on the order date
@pytest.mark.parametrize("days, action", [(60, "refund"), (61, "deny")], ids=["day 60", "day 61"])
def test_loyalty_window_boundary(engine, days, action):
    decision = engine.evaluate(make_request(loyalty_since=MEMBER_SINCE, days_after_delivery=days))

    assert decision.action_to_take == action
    assert decision.deadline == DELIVERED + timedelta(days=60)


def test_joined_on_order_date_counts(engine):
    decision = engine.evaluate(make_request(loyalty_since=ORDERED, days_after_delivery=45))

    assert decision.action_to_take == "refund"
    assert "from loyalty_window" in hit_for(decision, "C2").detail


def test_joined_after_order_date_does_not_count(engine):
    decision = engine.evaluate(
        make_request(loyalty_since=ORDERED + timedelta(days=1), days_after_delivery=45)
    )

    assert decision.action_to_take == "deny"
    assert decision.deadline == DELIVERED + timedelta(days=30)
    assert hit_for(decision, "loyalty_window").detail == (
        "Doesn't apply: joined Rewards on Feb 28, 2026, after the order date of Feb 27, 2026 (C4)."
    )


def test_non_member_has_no_loyalty_window(engine):
    decision = engine.evaluate(make_request())
    assert hit_for(decision, "loyalty_window").detail == "Doesn't apply: customer is not a Rewards member."


# -- S3 + C3 + C11: holiday orders (by order date, inclusive) get Jan 31 of the following year
@pytest.mark.parametrize(
    "ordered, deadline",
    [
        (date(2026, 11, 14), None),
        (date(2026, 11, 15), date(2027, 1, 31)),
        (date(2026, 12, 31), date(2027, 1, 31)),
        (date(2027, 1, 1), None),
    ],
    ids=["Nov 14", "Nov 15", "Dec 31", "Jan 1"],
)
def test_holiday_order_window_is_inclusive(config, ordered, deadline):
    request = make_request(order_date=ordered, delivery_date=ordered + timedelta(days=2))
    assert check_holiday_extension(config.rules.holiday_extension, request).deadline == deadline


@pytest.mark.parametrize(
    "requested, action",
    [(date(2027, 1, 31), "refund"), (date(2027, 2, 1), "deny")],
    ids=["Jan 31", "Feb 1"],
)
def test_holiday_deadline_is_next_year(engine, requested, action):
    """Ordered Dec 20, 2026: the standard window ends Jan 21, holiday extends to Jan 31, 2027."""
    decision = engine.evaluate(
        make_request(order_date=date(2026, 12, 20), delivery_date=date(2026, 12, 22), request_date=requested)
    )

    assert decision.action_to_take == action
    assert decision.deadline == date(2027, 1, 31)
    assert "from holiday_extension" in hit_for(decision, "C2").detail


# -- S5: defective items get 90 days
@pytest.mark.parametrize("days, action", [(90, "refund"), (91, "deny")], ids=["day 90", "day 91"])
def test_defective_window_boundary(engine, days, action):
    decision = engine.evaluate(make_request(defective=True, days_after_delivery=days))

    assert decision.action_to_take == action
    assert decision.deadline == DELIVERED + timedelta(days=90)


# -- C2: overlapping windows, the customer gets the latest deadline
def test_c2_member_holiday_order_gets_loyalty_deadline(engine):
    """The example from contradictions.md: bought Dec 20, delivered Dec 22, gets Feb 20, not Jan 31."""
    decision = engine.evaluate(
        make_request(
            loyalty_since=MEMBER_SINCE,
            order_date=date(2026, 12, 20),
            delivery_date=date(2026, 12, 22),
            request_date=date(2027, 2, 20),
        )
    )

    assert decision.action_to_take == "refund"
    assert decision.deadline == date(2027, 2, 20)
    assert "from loyalty_window." in hit_for(decision, "C2").detail


def test_c2_tie_names_every_window(engine):
    """Delivered Dec 2: loyalty (60 days) and holiday both end Jan 31, 2027."""
    decision = engine.evaluate(
        make_request(
            loyalty_since=MEMBER_SINCE,
            order_date=date(2026, 11, 20),
            delivery_date=date(2026, 12, 2),
            request_date=date(2027, 1, 31),
        )
    )

    assert decision.deadline == date(2027, 1, 31)
    assert hit_for(decision, "C2").detail == (
        "Latest deadline Jan 31, 2027, from loyalty_window and holiday_extension. "
        "Requested Jan 31, 2027, on or before it."
    )


def test_decision_records_policy_version(engine, config):
    assert engine.evaluate(make_request()).rules_version == config.policy_version


def test_window_length_comes_from_config(config):
    """Change the window in config, not code: a 45-day window ends 45 days after delivery."""
    # model_validate, not model_copy(update=...): model_copy skips validation,
    # so it would happily build a rule with days=-5.
    rule = config.rules.standard_window
    longer = WindowRule.model_validate({**rule.model_dump(), "days": 45})
    result = check_standard_window(longer, make_request(days_after_delivery=45))

    assert result.deadline == DELIVERED + timedelta(days=45)
