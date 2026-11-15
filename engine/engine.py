""" The engine that evaluates a request against the rules in rules.yaml """

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

from engine.config import HolidayRule, MonthDay, PolicyConfig, WindowRule
from engine.models import EngineRequest, PolicyDecision, RuleHit


@dataclass(frozen=True)
class WindowResult:
    """What a window check returns: the deadline it grants, plus its trace entries.

    deadline is None when the window doesn't apply; the hits then say why.
    """
    deadline: date | None
    hits: tuple[RuleHit, ...]
    rule_id: Literal["standard_window", "loyalty_window", "holiday_extension", "defective_window"]


def format_date(d: date) -> str:
    """Readable date for trace details, e.g. 'Apr 4, 2026'."""
    return f"{d:%b} {d.day}, {d.year}"


def format_month_day(md: MonthDay) -> str:
    """Readable month/day for trace details, e.g. 'Nov 15'."""
    return f"{date(2000, md.month, 1):%b} {md.day}"


# -- Window checks. Each one only reports the deadline it grants, as an "info" hit.
# -- Whether the request is in time is decided once, across all windows, by C2.

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
        detail = f"Item reported defective. Deadline {format_date(deadline)} ({rule.days} days from delivery)."

    hit = RuleHit(rule_id="defective_window", section=rule.section, result="info", detail=detail)
    return WindowResult(deadline=deadline, hits=(hit,), rule_id="defective_window")


def pick_latest_deadline(windows: tuple[WindowResult, ...], requested: date) -> tuple[date, RuleHit]:
    """C2: the customer gets the latest deadline among the windows that apply.

    Every window that produced that deadline is named, so a tie doesn't depend on call order.
    """
    # The standard window always applies, so there is always at least one deadline.
    latest = max(w.deadline for w in windows if w.deadline is not None)
    winners = " and ".join(w.rule_id for w in windows if w.deadline == latest)

    if requested <= latest:
        result = "pass"
        timing = "on or before it"
    else:
        result = "fail"
        timing = "after it"

    detail = f"Latest deadline {format_date(latest)}, from {winners}. Requested {format_date(requested)}, {timing}."
    return latest, RuleHit(rule_id="C2", section=None, result=result, detail=detail)


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
        deadline, c2_hit = pick_latest_deadline(windows, request.request_date)
        within_window = c2_hit.result == "pass"

        return PolicyDecision(
            policy_rules=tuple(hit for w in windows for hit in w.hits) + (c2_hit,),
            deadline=deadline,
            requires_human_review=False,
            action_to_take="refund" if within_window else "deny",
            rules_version=self.config.policy_version,
            refund_amount=request.order_fact.price_paid if within_window else None,  # C12
        )
