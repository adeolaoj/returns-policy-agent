from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from engine.config import PolicyConfig, WindowRule, load_policy
from engine.engine import (
    DEFECT_CLAIMED,
    PolicyEngine,
    WindowResult,
    check_holiday_extension,
    check_standard_window,
    check_window_gate,
)
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


# -- Trace shape: gates in precedence order (final sale, condition, window)
def test_trace_follows_precedence_order(engine):
    decision = engine.evaluate(make_request())

    assert [(h.rule_id, h.result) for h in decision.policy_rules] == [
        ("final_sale", "pass"),
        ("condition", "pass"),
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


def results(decision) -> list[tuple[str, str]]:
    """The trace as (rule_id, result) pairs."""
    return [(h.rule_id, h.result) for h in decision.policy_rules]


# -- S4 + C1: final sale blocks the return, unless the item is defective
def test_final_sale_not_defective_denied(engine):
    decision = engine.evaluate(make_request(is_final_sale=True))

    assert decision.action_to_take == "deny"
    assert decision.refund_amount is None
    assert decision.deadline is None  # no window applies to a non-defective final-sale item
    assert hit_for(decision, "final_sale").result == "fail"
    assert "C1" not in [h.rule_id for h in decision.policy_rules]
    # The window gate fails closed, but as "info": S4 already reports the real reason.
    assert hit_for(decision, "C2").result == "info"
    assert hit_for(decision, "C2").detail == (
        "No return window applies: final-sale item, not reported defective."
    )


def test_final_sale_defective_refunded_via_c1(engine):
    decision = engine.evaluate(make_request(is_final_sale=True, defective=True, days_after_delivery=90))

    assert decision.action_to_take == "refund"
    assert decision.deadline == DELIVERED + timedelta(days=90)
    assert hit_for(decision, "final_sale").result == "info"
    assert hit_for(decision, "C1").result == "pass"
    assert "fail" not in [h.result for h in decision.policy_rules]  # approved: no "fail" anywhere


def test_final_sale_defective_after_90_days_denied(engine):
    decision = engine.evaluate(make_request(is_final_sale=True, defective=True, days_after_delivery=91))

    assert decision.action_to_take == "deny"
    assert hit_for(decision, "C1").result == "pass"
    assert hit_for(decision, "C2").result == "fail"


def test_final_sale_only_counts_defective_window(config):
    """C1: with a 120-day loyalty window, a final-sale member still only gets the 90-day defective window.

    Uses a changed config, since with the real numbers the defective window always wins anyway.
    """
    data = config.model_dump()
    data["rules"]["loyalty_window"]["days"] = 120
    engine = PolicyEngine(PolicyConfig.model_validate(data))

    decision = engine.evaluate(
        make_request(is_final_sale=True, defective=True, loyalty_since=MEMBER_SINCE, days_after_delivery=100)
    )

    assert decision.action_to_take == "deny"
    assert decision.deadline == DELIVERED + timedelta(days=90)
    assert hit_for(decision, "loyalty_window").detail.endswith("(120 days from delivery).")  # still reported
    assert hit_for(decision, "C2").detail.startswith(
        "Final-sale item: only defective_window counts (C1). Latest deadline"
    )


# -- S7 + C5: unused with tags or packaging, unless defective
def test_used_item_denied(engine):
    decision = engine.evaluate(make_request(unused_with_tags_or_packaging=False))

    assert decision.action_to_take == "deny"
    assert decision.deadline == DELIVERED + timedelta(days=30)  # the window wasn't the problem
    assert hit_for(decision, "condition").result == "fail"
    assert hit_for(decision, "C2").result == "pass"


def test_used_defective_item_passes_condition(engine):
    decision = engine.evaluate(make_request(unused_with_tags_or_packaging=False, defective=True))

    assert decision.action_to_take == "refund"
    assert hit_for(decision, "condition").result == "pass"
    assert "S7's exception applies" in hit_for(decision, "condition").detail


# -- Run every gate: all failures are reported at once, in precedence order
def test_every_failing_gate_is_reported(engine):
    """Used, final sale, and late: the customer hears about all of it in one answer."""
    decision = engine.evaluate(
        make_request(is_final_sale=True, unused_with_tags_or_packaging=False, days_after_delivery=45)
    )

    assert decision.action_to_take == "deny"
    assert results(decision) == [
        ("final_sale", "fail"),
        ("condition", "fail"),
        ("standard_window", "info"),
        ("loyalty_window", "info"),
        ("holiday_extension", "info"),
        ("defective_window", "info"),
        ("C2", "info"),
    ]


# -- Fail closed: no eligible window never means approval
def test_window_gate_fails_closed_without_any_deadline():
    """If a future bug left a normal item with no windows, the gate must deny, not approve."""
    no_windows = tuple(
        WindowResult(deadline=None, hits=(), rule_id=rule_id)
        for rule_id in ("standard_window", "loyalty_window", "holiday_extension", "defective_window")
    )
    deadline, gate = check_window_gate(no_windows, make_request())

    assert deadline is None
    assert gate.passed is False
    assert gate.hits[0].detail == "No return window applies: no window granted a deadline."


# -- Audit: the defect phrase appears exactly where the unverified claim changed the outcome
def flagged(decision) -> list[str]:
    return [h.rule_id for h in decision.policy_rules if DEFECT_CLAIMED in h.detail]


@pytest.mark.parametrize(
    "overrides, expected",
    [
        ({}, []),
        ({"defective": True}, ["defective_window"]),
        ({"defective": True, "unused_with_tags_or_packaging": False}, ["condition", "defective_window"]),
        (
            {"defective": True, "unused_with_tags_or_packaging": False, "is_final_sale": True},
            ["C1", "condition", "defective_window"],
        ),
    ],
    ids=["no claim", "defective only", "defective and used", "defective, used, final sale"],
)
def test_defect_claim_is_flagged_where_it_matters(engine, overrides, expected):
    assert flagged(engine.evaluate(make_request(**overrides))) == expected
