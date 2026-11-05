from datetime import date
from typing import Literal
from pydantic import BaseModel
from decimal import Decimal


""" # LLM output model. Uses pydantic for validation and parsing of LLM output.
class ExtractedInfo(BaseModel):
    order_id: str
    intent: Literal["return", "exchange", "refund status", "out of scope"]
    details: str """


# Order facts: defines information about the order that is known.
class OrderFact(BaseModel):
    order_id: str
    order_date: date
    item: str
    product_price: float
    return_window_days: int
    is_final_sale: bool
    requested_in_stock: bool | None


# Customer claims: information about the order claimed by the customer.
# This is the information that the customer provides to the agent.
class CustomerClaim(BaseModel):
    claim_date: date
    return_reason: str
    item_condition: str


# Engine request: defines the input to the engine model, which includes order facts and customer claims.
class EngineRequest(BaseModel):
    order_fact: OrderFact
    customer_claim: CustomerClaim
    request_date: date


# Rule Hit: defines a rule that was hit during the evaluation of the engine model.
class RuleHit(BaseModel):
    rule_section: str
    rule_description: str
    rule_version: str
    valid_window: int | None

# Deterministic engine model
# This model is used to represent decisions made according to company policies and rules.
# Take cleaned LLM output and converts to structured data for further action

AllowedActions = Literal["refund", "store_credit", "exchange", "deny"]

class PolicyDecision(BaseModel):
    policy_rules: list[RuleHit] # List of policy rules that were evaluated to make the decision
    decision_date: date
    requires_human_review: bool
    action_to_take: AllowedActions  # Default action is to deny if no rules are met
    rules_version: str # Version of the policy rules applied
    refund_amount: Decimal