""" The engine that evaluates a request against the rules in rules.yaml """

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

from engine.config import HolidayRule, MonthDay, PolicyConfig, StandardRule, WindowRule
from engine.models import EngineRequest, PolicyDecision, RuleHit

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
# Within a gate, exactly one hit carries the pass/fail, and any others are "info".

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
        # final sale, then condition, then window. The trace follows the same order.
        final_sale_gate = check_final_sale(rules.final_sale, request)
        condition_gate = check_condition(rules.condition, request)
        window_hits = tuple(hit for w in windows for hit in w.hits)

        trace = final_sale_gate.hits + condition_gate.hits + window_hits + window_gate.hits
        approved = final_sale_gate.passed and condition_gate.passed and window_gate.passed

        return PolicyDecision(
            policy_rules=trace,
            deadline=deadline,
            requires_human_review=False,
            action_to_take="refund" if approved else "deny",
            rules_version=self.config.policy_version,
            refund_amount=request.order_fact.price_paid if approved else None,  # C12
        )
