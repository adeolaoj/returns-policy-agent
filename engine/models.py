from dataclasses import dataclass
from datetime import date
from typing import Literal
from pydantic import BaseModel


# LLM output model. Uses pydantic for validation and parsing of LLM output.
class LLMReturnContext(BaseModel):
    customer_id: str    # Needed for checking customer membership status
    order_id: str
    intent: str
    details: str
    return_amount: float


# Deterministic engine model
# This model is used to represent decisions made according to company policies and rules.
# Take cleaned LLM output and converts to structured data for further action

AllowedActions = Literal["refund", "store_credit", "exchange", "deny"]

@dataclass(frozen=True)
class PolicyDecision:
    policy_rules: list[str] # List of policy rules that were evaluated to make the decision
    decision_date: date
    action_to_take: AllowedActions = "deny"  # Default action is to deny if no rules are met
    rules_version: str = "1.0"  # Version of the policy rules applied
    refund_method: Literal["original_payment", "store_credit"] = "original_payment"  # Default refund method
    refund_amount: float = 0.0  # Default refund amount is 0.0, can be updated based on policy rules