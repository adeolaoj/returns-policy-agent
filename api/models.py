"""Response models: the shape the API promises its callers.

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

from pydantic import BaseModel


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
