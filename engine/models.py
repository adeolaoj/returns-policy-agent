from datetime import date
from typing import Literal
from pydantic import BaseModel, ConfigDict
from decimal import Decimal


# Order facts: defines information about the order that is known.
class OrderFact(BaseModel):
    order_id: str
    order_date: date
    delivery_date: date
    customer_membership_status: Literal["member", "non-member"]
    item: str
    price_paid: Decimal
    is_final_sale: bool
    is_gift: bool
    customer_variant: str

# Exchange request: defines information about the exchange request that is known.
class ExchangeRequest(BaseModel):
    requested_variant: str
    requested_in_stock: bool

# Customer claims: information about the order claimed by the customer.
# This is the information that the customer provides to the agent.
class CustomerClaim(BaseModel):
    defective: bool
    exchange_request: ExchangeRequest | None


# Engine request: defines the input to the engine model, which includes order facts and customer claims.
class EngineRequest(BaseModel):
    order_fact: OrderFact
    customer_claim: CustomerClaim
    request_date: date


# Rule Hit: defines a rule that was hit during the evaluation of the engine model.
class RuleHit(BaseModel):
    rule_section: str
    rule_description: str
    valid_window: int | None
    result: Literal["pass", "fail", "info"]

# Deterministic engine model
# This model is used to represent decisions made according to company policies and rules.
# Take cleaned LLM output and converts to structured data for further action

AllowedActions = Literal["refund", "store_credit", "exchange", "deny"]

class PolicyDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)  # Forbid extra fields in the model
    policy_rules: list[RuleHit] # List of policy rules that were evaluated to make the decision
    deadline: date
    requires_human_review: bool
    action_to_take: AllowedActions
    rules_version: str # Version of the policy rules applied
    refund_amount: Decimal | None