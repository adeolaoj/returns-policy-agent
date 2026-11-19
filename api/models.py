"""API models: the shape of what callers send (writes) and what the API promises back.

These are separate from the engine's models (engine/models.py). They describe what the backend
sends, not what the engine needs. The agent's lookup_order node converts one into the other.

Types only, no cross-field validators: the API doesn't judge its data (FG-1011 must be served).
If a stored value has the wrong *type*, FastAPI fails the response with a 500. That's the API
failing, and the agent handles it like any other outage.

Field names must match data/*.json, since routes return stored records and FastAPI reads
fields from them by name. Stored fields a model doesn't list are left out of the response.
"""
from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class LineItem(BaseModel):
    line_id: str
    product_id: str
    name: str
    size: str | None  # None for products without sizes (scarf, lamp)
    color: str | None
    price_paid: Decimal  # stored as a string ("68.00"), and sent back as one
    is_final_sale: bool


class Order(BaseModel):
    id: str
    customer_id: str
    ordered_at: date
    delivered_at: date | None
    is_gift: bool
    shipping_cost: Decimal
    items: list[LineItem]


class Customer(BaseModel):
    id: str
    name: str
    email: str
    loyalty_since: date | None


class VariantStock(BaseModel):
    """One variant as the API exposes it: in_stock. The raw quantity is not exposed."""
    size: str | None
    color: str | None
    in_stock: bool


class Inventory(BaseModel):
    """GET /inventory/{product_id}: the product and the variants that matched the filters."""
    product_id: str
    name: str
    variants: list[VariantStock]


# -- Write requests: what a caller sends.
# Unlike stored data, a request is checked strictly: the API refuses a malformed request (422)
# rather than guessing what was meant. FastAPI does this automatically for a route's body model.
class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


RefundMethod = Literal["original_payment", "store_credit"]
EscalationReason = Literal["approval_required", "data_problem", "service_unavailable",
                           "customer_request"]


class RefundCreate(StrictRequest):
    order_id: str
    line_id: str
    amount: Decimal = Field(gt=0, decimal_places=2)
    # Store credit is a refund paid to a different place (C7), so it's a method, not a resource.
    method: RefundMethod
    # Set by the caller from the engine's decision (S8). The API trusts it, so the threshold
    # lives only in rules.yaml (see the Assumptions in contradictions.md).
    requires_approval: bool


class ExchangeCreate(StrictRequest):
    order_id: str
    line_id: str
    size: str | None
    color: str | None


class EscalationCreate(StrictRequest):
    reason: EscalationReason
    order_id: str | None = None  # an outage escalation may happen before any order is known
    refund_id: str | None = None
    context: str = Field(min_length=1)

    # Only an approval escalation needs a refund: it asks a person to approve that refund.
    # A data_problem or service_unavailable escalation has no refund to point to.
    @model_validator(mode="after")
    def validate_approval_has_refund(self):
        if self.reason == "approval_required" and self.refund_id is None:
            raise ValueError("an approval_required escalation needs a refund_id")
        return self


# -- Write responses: records the server built. Routes construct these (the "build the
# response" case), since IDs and statuses don't exist until the server creates them.

class Refund(BaseModel):
    refund_id: str
    order_id: str
    line_id: str
    amount: Decimal
    method: RefundMethod
    status: Literal["issued", "pending_approval"]


class Exchange(BaseModel):
    exchange_id: str
    order_id: str
    line_id: str
    product_id: str
    size: str | None
    color: str | None
    status: Literal["created"]


class Escalation(BaseModel):
    ticket_id: str
    reason: EscalationReason
    order_id: str | None
    refund_id: str | None
    context: str
    status: Literal["pending"]
