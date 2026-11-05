from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


# Base for every input model. Inputs carry untrusted data (customer claims, LLM output,
# upstream systems), so we validate at the beginning before continuing
class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


# Variant: identifies a specific version of a product, so the engine can check
# "same product, different size or color" (C9) without parsing free text.
# size/color are optional because some items (e.g. home goods) don't have them.
class Variant(StrictInput):
    product_id: str
    size: str | None = None
    color: str | None = None


# Order facts: information about the order from our own order system (trusted).
class OrderFact(StrictInput):
    order_id: str
    order_date: date
    delivery_date: date
    # Date the customer joined the rewards program, or None if never a member.
    # The engine applies C4 itself (member as of order_date)
    loyalty_since: date | None
    item: str
    price_paid: Decimal = Field(gt=0)
    is_final_sale: bool
    is_gift: bool
    purchased_variant: Variant

    @model_validator(mode="after")
    def validate_dates(self):
        if self.delivery_date < self.order_date:
            raise ValueError("delivery_date cannot be before order_date")
        return self


# Customer claims: information supplied by the customer (untrusted, unverified).
class CustomerClaim(StrictInput):
    defective: bool
    # S7 / C5: item is unused and has its tags or original packaging.
    unused_with_tags_or_packaging: bool


# Exchange request: a third source of data. The customer chooses the variant;
# the inventory system answers whether it's in stock.
class ExchangeRequest(StrictInput):
    requested_variant: Variant  # supplied by the customer
    requested_in_stock: bool  # supplied by the inventory system


# Engine request: the full input to the engine from every source.
class EngineRequest(StrictInput):
    order_fact: OrderFact
    customer_claim: CustomerClaim
    exchange_request: ExchangeRequest | None = None
    request_date: date

    # A request made before delivery is invalid input, not a policy decision:
    # it means something upstream called the engine when it shouldn't have.
    # So, it gets rejected here so the engine
    @model_validator(mode="after")
    def validate_request_date(self):
        if self.request_date < self.order_fact.delivery_date:
            raise ValueError("request_date cannot be before order_fact.delivery_date")
        return self


# Rule Hit: a rule evaluated while making this decision.
class RuleHit(BaseModel):
    model_config = ConfigDict(frozen=True)
    rule_id: str
    section: str | None = None  # policy section, e.g. "S4"; None for contradiction rulings
    result: Literal["pass", "fail", "info"]
    # Explanation specific to this decision, for the trace panel,
    # e.g. "Requested Apr 5, after the deadline of Apr 4."
    detail: str


# Deterministic engine output.
# Built only by internal engine code;
# frozen keeps decision from being changed after it's made. frozen is shallow,
# so policy_rules is a tuple of frozen RuleHits to make the whole decision immutable.

AllowedActions = Literal["refund", "store_credit", "exchange", "deny"]

class PolicyDecision(BaseModel):
    model_config = ConfigDict(frozen=True)
    policy_rules: tuple[RuleHit, ...]  # Rules evaluated to make the decision
    deadline: date
    requires_human_review: bool
    action_to_take: AllowedActions
    rules_version: str  # Version of the policy rules applied
    refund_amount: Decimal | None
