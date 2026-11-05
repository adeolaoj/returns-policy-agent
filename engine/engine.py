""" The engine that evaluates a request against the rules in rules.yaml """

from dataclasses import dataclass
from datetime import date, timedelta

from engine.config import PolicyConfig, WindowRule
from engine.models import EngineRequest, PolicyDecision, RuleHit


@dataclass(frozen=True)
class WindowResult:
    """What a window check returns: the deadline it grants, plus its trace entries."""
    deadline: date
    hits: tuple[RuleHit, ...]


def format_date(d: date) -> str:
    """Readable date for trace details, e.g. 'Apr 4, 2026'."""
    return f"{d:%b} {d.day}, {d.year}"


def check_standard_window(rule: WindowRule, request: EngineRequest) -> WindowResult:
    """S1: returns are accepted within N days of delivery, counting day N itself (C11)."""
    delivered = request.order_fact.delivery_date
    deadline = delivered + timedelta(days=rule.days)
    requested = request.request_date

    if requested <= deadline:
        result = "pass"
        detail = f"Requested {format_date(requested)}, on or before the deadline of {format_date(deadline)}."
    else:
        result = "fail"
        detail = f"Requested {format_date(requested)}, after the deadline of {format_date(deadline)}."

    hit = RuleHit(rule_id="standard_window", section=rule.section, result=result, detail=detail)
    return WindowResult(deadline=deadline, hits=(hit,))


class PolicyEngine:
    """Applies a loaded policy config to return requests."""

    def __init__(self, config: PolicyConfig):
        self.config = config

    def evaluate(self, request: EngineRequest) -> PolicyDecision:
        """Evaluate a request against the policy and return a decision."""
        standard = check_standard_window(self.config.rules.standard_window, request)
        within_window = request.request_date <= standard.deadline

        return PolicyDecision(
            policy_rules=standard.hits,
            deadline=standard.deadline,
            requires_human_review=False,
            action_to_take="refund" if within_window else "deny",
            rules_version=self.config.policy_version,
            refund_amount=request.order_fact.price_paid if within_window else None,  # C12
        )
