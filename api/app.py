"""The mock Fernhill backend.

create_app builds a fresh app around the seed data it's given. Nothing is global, so each test
gets its own app and its own data. That matters once step 3 adds writes.
"""
import copy
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Annotated

from fastapi import FastAPI, Header, HTTPException

from api.idempotency import IdempotencyStore
from api.models import (
    Customer, Escalation, EscalationCreate, Exchange, ExchangeCreate, Inventory, Order, Refund,
    RefundCreate, VariantStock,
)
from api.seed import Seed


def not_found(code: str, message: str) -> HTTPException:
    """A 404 with a machine-readable code the agent can route on.

    FastAPI sends it as {"detail": {"code": ..., "message": ...}}.
    """
    return HTTPException(status_code=404, detail={"code": code, "message": message})


def conflict(code: str, message: str) -> HTTPException:
    """A 409: the request is valid, but the current state doesn't allow it (out of stock, ...)."""
    return HTTPException(status_code=409, detail={"code": code, "message": message})


def unprocessable(code: str, message: str) -> HTTPException:
    """A 422 for a request that's well-formed but makes no sense (refunding more than was paid)."""
    return HTTPException(status_code=422, detail={"code": code, "message": message})


# A required header. FastAPI reads "Idempotency-Key" for a parameter named idempotency_key,
# and answers 422 when it's missing.
IdempotencyKey = Annotated[str, Header()]


@dataclass
class Records:
    """What the writes create. Like the seed, it lives inside one app."""
    refunds: dict[str, Refund] = field(default_factory=dict)
    exchanges: dict[str, Exchange] = field(default_factory=dict)
    escalations: dict[str, Escalation] = field(default_factory=dict)
    # (order_id, line_id) -> the refund_id or exchange_id that resolved that line.
    # A line is resolved once: the backend is the only part that remembers past payouts.
    resolved_lines: dict[tuple[str, str], str] = field(default_factory=dict)


def next_id(prefix: str, existing: dict) -> str:
    """Sequential IDs per app: RF-0001, RF-0002, ..."""
    return f"{prefix}-{len(existing) + 1:04d}"


def create_app(seed: Seed) -> FastAPI:
    # Copy, so this app owns its data: a write in one app can't change the caller's Seed, or
    # another app built from the same Seed.
    seed = copy.deepcopy(seed)
    records = Records()
    idempotency = IdempotencyStore()
    app = FastAPI(title="Fernhill mock backend")

    # The routes are defined inside create_app so they can read `seed` (a closure).
    # response_model tells FastAPI to validate and shape the output with that model.

    @app.get("/orders/{order_id}", response_model=Order)
    def get_order(order_id: str):
        order = seed.orders.get(order_id)
        if order is None:
            raise not_found("order_not_found", f"No order with id {order_id!r}.")
        return order

    @app.get("/customers/{customer_id}", response_model=Customer)
    def get_customer(customer_id: str):
        customer = seed.customers.get(customer_id)
        if customer is None:
            raise not_found("customer_not_found", f"No customer with id {customer_id!r}.")
        return customer

    @app.get("/inventory/{product_id}", response_model=Inventory)
    def get_inventory(product_id: str, size: str | None = None, color: str | None = None):
        # FastAPI reads size and color from the query string (?size=L&color=white), and they're
        # None when left out. None means "any". A filter that matches nothing is a 200 with an
        # empty variants list, not a 404: the product exists, and nothing matched.
        
        product = seed.inventory.get(product_id)

        if product is None:
            raise not_found("product_not_found", f"No product with id {product_id!r}.")

        variants = []
        for variant in product["variants"]:
            if (size is None or variant["size"] == size) and (color is None or variant["color"] == color):
                variants.append(VariantStock(
                    size=variant["size"],
                    color=variant["color"],
                    in_stock=variant["quantity"] > 0
                ))
        return Inventory(
            product_id=product["product_id"],
            name=product["name"],
            variants=variants
        )

    # -- Write helpers, shared by /refunds and /exchanges

    def find_line(order_id: str, line_id: str) -> dict:
        """The stored line item, or a 404 saying whether the order or the line is missing."""
        order = seed.orders.get(order_id)

        if order is None:
            raise not_found("order_not_found", f"No order with id {order_id!r}.")

        for item in order["items"]:
            if item["line_id"] == line_id:
                return item
            
        raise not_found("line_not_found", f"No line with id {line_id!r} in order {order_id!r}.")

    def check_unresolved(order_id: str, line_id: str) -> None:
        """409 if this line already has a refund or exchange."""
        if (order_id, line_id) in records.resolved_lines:
            existing_id = records.resolved_lines[(order_id, line_id)]
            raise conflict(
                "line_already_resolved",
                f"Line {line_id!r} in order {order_id!r} is already resolved by {existing_id!r}."
            )

    # -- Writes. Each follows the same four steps; create_escalation is the worked example.
    # status_code=201 means "Created". A replay returns 201 too: same request, same answer.

    @app.post("/refunds", status_code=201, response_model=Refund)
    def create_refund(body: RefundCreate, idempotency_key: IdempotencyKey):
        # 1. Replay.
        replay = idempotency.replay("/refunds", idempotency_key, body)
        if replay is not None:
            return replay

        # 2. Check. The helpers raise the right HTTPException themselves (404 with
        # order_not_found or line_not_found, 409 with line_already_resolved), and FastAPI turns
        # it into the response, so there's nothing to catch here.
        item = find_line(body.order_id, body.line_id)
        check_unresolved(body.order_id, body.line_id)

        if body.amount > Decimal(item["price_paid"]):
            raise unprocessable(
                "amount_exceeds_price_paid",
                f"Refund amount {body.amount} exceeds price paid {item['price_paid']} for line {body.line_id!r}."
            )

        # 3. Act: create the refund, and mark the line so it can't be refunded or exchanged again.
        refund = Refund(
            refund_id=next_id("RF", records.refunds),
            order_id=body.order_id,
            line_id=body.line_id,
            amount=body.amount,
            method=body.method,
            status="pending_approval" if body.requires_approval else "issued"
        )
        records.refunds[refund.refund_id] = refund
        records.resolved_lines[(body.order_id, body.line_id)] = refund.refund_id

        # 4. Save, so a retry replays this instead of acting again.
        idempotency.save("/refunds", idempotency_key, body, refund)
        return refund

    @app.post("/exchanges", status_code=201, response_model=Exchange)
    def create_exchange(body: ExchangeCreate, idempotency_key: IdempotencyKey):
        # 1. Replay.
        replay = idempotency.replay("/exchanges", idempotency_key, body)
        if replay is not None:
            return replay

        # 2. Check.
        item = find_line(body.order_id, body.line_id)
        check_unresolved(body.order_id, body.line_id)

        product = seed.inventory.get(item["product_id"])
        if product is None:  # the discontinued tote
            raise not_found("product_not_found", f"No product with id {item['product_id']!r}.")

        # Exact match on both, so None == None counts (the scarf has no size).
        variant = next(
            (v for v in product["variants"] if v["size"] == body.size and v["color"] == body.color),
            None,
        )
        if variant is None:
            raise not_found(
                "variant_not_found",
                f"{product['name']} has no variant with size {body.size!r} and color {body.color!r}."
            )
        # The engine saw a snapshot of stock; the backend holds the truth.
        if variant["quantity"] <= 0:
            raise conflict("out_of_stock", f"{product['name']} ({body.size}, {body.color}) is out of stock.")

        # 3. Act: take one from stock, create the exchange, mark the line.
        variant["quantity"] -= 1
        exchange = Exchange(
            exchange_id=next_id("EX", records.exchanges),
            order_id=body.order_id,
            line_id=body.line_id,
            product_id=item["product_id"],
            size=body.size,
            color=body.color,
            status="created",
        )
        records.exchanges[exchange.exchange_id] = exchange
        records.resolved_lines[(body.order_id, body.line_id)] = exchange.exchange_id

        # 4. Save and return.
        idempotency.save("/exchanges", idempotency_key, body, exchange)
        return exchange

    @app.post("/escalations", status_code=201, response_model=Escalation)
    def create_escalation(body: EscalationCreate, idempotency_key: IdempotencyKey):
        # 1. Replay: a retry of a request we already handled gets the stored response.
        stored = idempotency.replay("/escalations", idempotency_key, body)
        if stored is not None:
            return stored

        # 2. Check: every reference must point at something real.
        if body.order_id is not None and body.order_id not in seed.orders:
            raise not_found("order_not_found", f"No order with id {body.order_id!r}.")
        if body.refund_id is not None and body.refund_id not in records.refunds:
            raise not_found("refund_not_found", f"No refund with id {body.refund_id!r}.")

        # 3. Act. **body.model_dump() passes reason, order_id, refund_id and context through.
        escalation = Escalation(
            ticket_id=next_id("ESC", records.escalations), status="pending", **body.model_dump()
        )
        records.escalations[escalation.ticket_id] = escalation

        # 4. Save the success. Every failure raised above, before this line, so failures are
        # never stored and a retry runs again.
        idempotency.save("/escalations", idempotency_key, body, escalation)
        return escalation

    return app
