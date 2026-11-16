""" The engine that evaluates a request against the rules in rules.yaml """

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

from engine.config import HolidayRule, MonthDay, PolicyConfig, StandardRule, WindowRule
from engine.models import EngineRequest, PolicyDecision, RuleHit, Variant

# Every hit whose outcome depends on the customer's (unverified) defect claim includes this
# exact phrase, so an auditor can find those decisions with one search.
DEFECT_CLAIMED = "(defect claimed, unverified)"


@dataclass(frozen=True)
class WindowResult:
    """What a window check returns: the deadline it grants, plus its trace entries.
    deadline is None when the window doesn't apply; the hits then say why.
    """
    deadline: date | None
    hits: tuple[RuleHit, ...]
    rule_id: Literal["standard_window", "loyalty_window", "holiday_extension", "defective_window"]


@dataclass(frozen=True)
class GateResult:
    """What a gate check returns: whether the request passes, plus its trace entries.
    The hits always say why it passed or failed.
    """
    passed: bool
    hits: tuple[RuleHit, ...]

def format_date(d: date) -> str:
    """Readable date for trace details, e.g. 'Apr 4, 2026'."""
    return f"{d:%b} {d.day}, {d.year}"


def format_month_day(md: MonthDay) -> str:
    """Readable month/day for trace details, e.g. 'Nov 15'."""
    return f"{date(2000, md.month, 1):%b} {md.day}"


# Window checks. Each one only reports the deadline it grants, as an "info" hit.
# Whether the request is in time is decided once, across all windows, by C2.

def check_standard_window(rule: WindowRule, request: EngineRequest) -> WindowResult:
    """S1: everyone gets N days from delivery (C3)."""
    delivered = request.order_fact.delivery_date
    deadline = delivered + timedelta(days=rule.days)
    detail = f"Deadline {format_date(deadline)} ({rule.days} days from delivery on {format_date(delivered)})."

    hit = RuleHit(rule_id="standard_window", section=rule.section, result="info", detail=detail)
    return WindowResult(deadline=deadline, hits=(hit,), rule_id="standard_window")


def check_loyalty_window(rule: WindowRule, request: EngineRequest) -> WindowResult:
    """S2: Rewards members get N days from delivery, if they were members on the order date (C4)."""
    order = request.order_fact
    joined = order.loyalty_since

    if joined is None:
        deadline = None
        detail = "Doesn't apply: customer is not a Rewards member."
    elif joined > order.order_date:
        # C4: membership counts as of the order date, so joining later doesn't extend the window.
        deadline = None
        detail = (
            f"Doesn't apply: joined Rewards on {format_date(joined)}, "
            f"after the order date of {format_date(order.order_date)} (C4)."
        )
    else:
        deadline = order.delivery_date + timedelta(days=rule.days)
        detail = (
            f"Member since {format_date(joined)}, on or before the order date of "
            f"{format_date(order.order_date)} (C4). Deadline {format_date(deadline)} "
            f"({rule.days} days from delivery)."
        )

    hit = RuleHit(rule_id="loyalty_window", section=rule.section, result="info", detail=detail)
    return WindowResult(deadline=deadline, hits=(hit,), rule_id="loyalty_window")


def check_holiday_extension(rule: HolidayRule, request: EngineRequest) -> WindowResult:
    """S3: items ordered between start and end (inclusive, C11) get a fixed deadline the following year.

    Counts from the order date, not delivery (C3).
    """
    ordered = request.order_fact.order_date
    window = f"{format_month_day(rule.order_start)} to {format_month_day(rule.order_end)}"
    in_window = rule.order_start.as_tuple() <= (ordered.month, ordered.day) <= rule.order_end.as_tuple()

    if not in_window:
        deadline = None
        detail = f"Doesn't apply: ordered {format_date(ordered)}, outside {window}."
    else:
        # Assumption: the order window falls within one calendar year (it never crosses Jan 1),
        # so the deadline is always in the year after the order. HolidayRule.check_order_window
        # in config.py guarantees this by rejecting a window whose start is after its end.
        deadline = date(ordered.year + 1, rule.deadline.month, rule.deadline.day)
        detail = f"Ordered {format_date(ordered)}, within {window}. Deadline {format_date(deadline)}."

    hit = RuleHit(rule_id="holiday_extension", section=rule.section, result="info", detail=detail)
    return WindowResult(deadline=deadline, hits=(hit,), rule_id="holiday_extension")


def check_defective_window(rule: WindowRule, request: EngineRequest) -> WindowResult:
    """S5: defective items get N days from delivery."""
    if not request.customer_claim.defective:
        deadline = None
        detail = "Doesn't apply: item not reported defective."
    else:
        deadline = request.order_fact.delivery_date + timedelta(days=rule.days)
        detail = (
            f"Item reported defective {DEFECT_CLAIMED}. "
            f"Deadline {format_date(deadline)} ({rule.days} days from delivery)."
        )

    hit = RuleHit(rule_id="defective_window", section=rule.section, result="info", detail=detail)
    return WindowResult(deadline=deadline, hits=(hit,), rule_id="defective_window")


# Gate checks. Each gate owns its verdict (GateResult.passed); its hits only explain it.
# A passing gate has exactly one "pass" hit; a failing gate has one "fail" hit per
# independent problem. Any other hits are "info".

def check_final_sale(rule: StandardRule, request: EngineRequest) -> GateResult:
    """S4: final-sale items can't be returned, unless they're defective (C1)."""
    if not request.order_fact.is_final_sale:
        hit = RuleHit(rule_id="final_sale", section=rule.section, result="pass", detail="Not a final-sale item.")
        return GateResult(passed=True, hits=(hit,))

    if not request.customer_claim.defective:
        hit = RuleHit(
            rule_id="final_sale", section=rule.section, result="fail",
            detail="Final-sale item, not reported defective: it can't be returned.",
        )
        return GateResult(passed=False, hits=(hit,))

    # C1: defective overrides final sale. S4 is only "info" here; C1 carries the verdict,
    # so an approved decision never contains a "fail".
    s4 = RuleHit(rule_id="final_sale", section=rule.section, result="info", detail="Final-sale item.")
    c1 = RuleHit(
        rule_id="C1", section=None, result="pass",
        detail=f"Item reported defective {DEFECT_CLAIMED}, so final sale doesn't block the return.",
    )
    return GateResult(passed=True, hits=(s4, c1))


def check_condition(rule: StandardRule, request: EngineRequest) -> GateResult:
    """S7 + C5: the item must be unused with tags or original packaging, unless it's defective."""
    if request.customer_claim.unused_with_tags_or_packaging:
        # Checked first, so the defect claim is only cited when it actually changed the outcome.
        result, passed = "pass", True
        detail = "Unused, with tags or original packaging (C5)."
    elif request.customer_claim.defective:
        result, passed = "pass", True
        detail = f"Used or missing tags/packaging, but reported defective {DEFECT_CLAIMED}, so S7's exception applies."
    else:
        result, passed = "fail", False
        detail = "Used, or missing tags and original packaging (C5)."

    hit = RuleHit(rule_id="condition", section=rule.section, result=result, detail=detail)
    return GateResult(passed=passed, hits=(hit,))


def check_window_gate(windows: tuple[WindowResult, ...], request: EngineRequest) -> tuple[date | None, GateResult]:
    """C2: the customer gets the latest deadline among the eligible windows.

    Final-sale items only get the defective window (C1); every window still reports, but only
    eligible ones are compared. Every window that produced the latest deadline is named, so a
    tie doesn't depend on call order. Returns the deadline (None if no window is eligible) and
    the gate result.
    """
    final_sale = request.order_fact.is_final_sale
    eligible = [
        w for w in windows
        if w.deadline is not None and (not final_sale or w.rule_id == "defective_window")
    ]
    scope = "Final-sale item: only defective_window counts (C1). " if final_sale else ""

    if not eligible:
        # Fail closed: with no deadline there's nothing to be "in time" for. The reason itself
        # (e.g. final sale) is reported by its own gate, so this hit is "info", not a second "fail".
        reason = "final-sale item, not reported defective" if final_sale else "no window granted a deadline"
        hit = RuleHit(rule_id="C2", section=None, result="info", detail=f"No return window applies: {reason}.")
        return None, GateResult(passed=False, hits=(hit,))

    latest = max(w.deadline for w in eligible if w.deadline is not None)  # all eligible have one
    winners = " and ".join(w.rule_id for w in eligible if w.deadline == latest)
    requested = request.request_date

    if requested <= latest:
        result, passed, timing = "pass", True, "on or before it"
    else:
        result, passed, timing = "fail", False, "after it"

    detail = (
        f"{scope}Latest deadline {format_date(latest)}, from {winners}. "
        f"Requested {format_date(requested)}, {timing}."
    )
    hit = RuleHit(rule_id="C2", section=None, result=result, detail=detail)
    return latest, GateResult(passed=passed, hits=(hit,))


def format_variant(v: Variant) -> str:
    """Readable variant for trace details, e.g. 'size M, color blue' or 'no size, color oak'."""
    size = f"size {v.size}" if v.size is not None else "no size"
    color = f"color {v.color}" if v.color is not None else "no color"
    return f"{size}, {color}"


def check_exchange(rule: StandardRule, request: EngineRequest) -> GateResult:
    """S9 + C9: an exchange must be the same product, change size and/or color, and be in stock.

    Plain returns (no exchange_request) pass with no hits: exchange rules can't block a return,
    and nobody returning an item wonders about them, so they'd only clutter the trace.

    Variants are compared exactly as given. The engine assumes the requested variant is a real
    catalog variant with values normalized upstream (see contradictions.md, Assumptions), and it
    reads stock from the inventory system's answer rather than looking anything up itself.
    """
    exchange = request.exchange_request
    if exchange is None:
        return GateResult(passed=True, hits=())

    purchased = request.order_fact.purchased_variant
    wanted = exchange.requested_variant

    # C8: the final-sale, condition and window gates above already apply to this exchange.
    c8 = RuleHit(
        rule_id="C8", section=None, result="info",
        detail="Exchange: the same final-sale, condition and window rules as a return apply.",
    )

    failures = []
    if wanted.product_id != purchased.product_id:
        # Size/color aren't compared across different products: that comparison means nothing.
        failures.append(f"Requested product {wanted.product_id}, not the purchased product {purchased.product_id} (C9).")
    elif (wanted.size, wanted.color) == (purchased.size, purchased.color):
        failures.append(
            f"Requested the same variant as purchased ({format_variant(purchased)}); "
            "an exchange must change the size, the color, or both (C9)."
        )
    # Stock is independent of the product check, so it's always reported.
    if not exchange.requested_in_stock:
        failures.append(f"Requested variant ({format_variant(wanted)}) is out of stock.")

    if failures:
        hits = tuple(
            RuleHit(rule_id="exchange", section=rule.section, result="fail", detail=d) for d in failures
        )
        return GateResult(passed=False, hits=(c8,) + hits)

    detail = (
        f"Same product {wanted.product_id}, from {format_variant(purchased)} "
        f"to {format_variant(wanted)}, in stock (C9)."
    )
    hit = RuleHit(rule_id="exchange", section=rule.section, result="pass", detail=detail)
    return GateResult(passed=True, hits=(c8, hit))


class PolicyEngine:
    """Applies a loaded policy config to return requests."""

    def __init__(self, config: PolicyConfig):
        self.config = config

    def evaluate(self, request: EngineRequest) -> PolicyDecision:
        """Evaluate a request against the policy and return a decision."""
        rules = self.config.rules
        windows = (
            check_standard_window(rules.standard_window, request),
            check_loyalty_window(rules.loyalty_window, request),
            check_holiday_extension(rules.holiday_extension, request),
            check_defective_window(rules.defective_window, request),
        )
        deadline, window_gate = check_window_gate(windows, request)

        # Every gate runs, every time, in the precedence order from contradictions.md:
        # final sale, condition, window, then exchange rules. The trace follows the same order.
        final_sale_gate = check_final_sale(rules.final_sale, request)
        condition_gate = check_condition(rules.condition, request)
        window_hits = tuple(hit for w in windows for hit in w.hits)
        exchange_gate = check_exchange(rules.exchange, request)

        gates = (final_sale_gate, condition_gate, window_gate, exchange_gate)
        trace = final_sale_gate.hits + condition_gate.hits + window_hits + window_gate.hits + exchange_gate.hits
        approved = all(g.passed for g in gates)

        # The customer chose return or exchange; the engine only checks that choice and never
        # switches it. A denied exchange's trace still shows whether a return would pass.
        if not approved:
            action, refund_amount = "deny", None
        elif request.exchange_request is not None:
            action, refund_amount = "exchange", None  # no money is paid out
        else:
            action, refund_amount = "refund", request.order_fact.price_paid  # C12

        return PolicyDecision(
            policy_rules=trace,
            deadline=deadline,
            requires_human_review=False,
            action_to_take=action,
            rules_version=self.config.policy_version,
            refund_amount=refund_amount,
        )
