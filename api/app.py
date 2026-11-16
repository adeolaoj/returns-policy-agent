"""The mock Fernhill backend.

create_app builds a fresh app around the seed data it's given. Nothing is global, so each test
gets its own app and its own data. That matters once step 3 adds writes.
"""
import copy

from fastapi import FastAPI, HTTPException

from api.models import Customer, Inventory, Order, VariantStock
from api.seed import Seed


def not_found(code: str, message: str) -> HTTPException:
    """A 404 with a machine-readable code the agent can route on.

    FastAPI sends it as {"detail": {"code": ..., "message": ...}}.
    """
    return HTTPException(status_code=404, detail={"code": code, "message": message})


def create_app(seed: Seed) -> FastAPI:
    # Copy, so this app owns its data: a write in one app (step 3) can't change the caller's
    # Seed, or another app built from the same Seed.
    seed = copy.deepcopy(seed)
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

    return app
