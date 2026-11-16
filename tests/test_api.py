from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.seed import build_seed, load_seed

DATA_DIR = Path(__file__).parent.parent / "data"


@pytest.fixture
def client() -> TestClient:
    """A fresh app built from the real seed data, so no test sees another test's changes."""
    return TestClient(create_app(load_seed(DATA_DIR)))


def error_code(response) -> str:
    """The machine-readable code in a 404 body: {"detail": {"code": ..., "message": ...}}."""
    return response.json()["detail"]["code"]


# -- Loading the seed
def test_load_real_seed():
    seed = load_seed(DATA_DIR)
    assert len(seed.orders) == 11
    assert len(seed.customers) == 3
    assert len(seed.inventory) == 7
    assert seed.orders["FG-1001"]["customer_id"] == "CUST-002"
    assert seed.inventory["P-LAMP-CERAMIC"]["name"] == "Ceramic table lamp"


@pytest.mark.parametrize(
    "orders, customers, inventory, message",
    [
        ([{"id": "FG-1"}, {"id": "FG-1"}], [], [], "duplicate order id 'FG-1'"),
        ([], [{"id": "C-1"}, {"id": "C-1"}], [], "duplicate customer id 'C-1'"),
        ([], [], [{"product_id": "P-1"}, {"product_id": "P-1"}], "duplicate product id 'P-1'"),
    ],
    ids=["order", "customer", "product"],
)
def test_duplicate_id_rejected(orders, customers, inventory, message):
    """A duplicate ID would silently replace the first record in the index, so it's an error."""
    with pytest.raises(ValueError, match=message):
        build_seed(orders, customers, inventory)


@pytest.mark.parametrize(
    "inventory",
    [[{"product_id": "P-1"}, {"name": "no id"}], [{"product_id": "P-1"}, "not a record"]],
    ids=["missing key", "not an object"],
)
def test_record_without_id_rejected(inventory):
    """The error names the record instead of raising a bare KeyError or TypeError."""
    with pytest.raises(ValueError, match="product record #2 has no 'product_id'"):
        build_seed([], [], inventory)


def test_non_list_file_rejected(tmp_path: Path):
    """A file whose top level isn't a list is reported by name."""
    for name in ("orders.json", "customers.json", "inventory.json"):
        (tmp_path / name).write_text("[]", encoding="utf-8")
    (tmp_path / "customers.json").write_text('{"id": "CUST-001"}', encoding="utf-8")

    with pytest.raises(ValueError, match="customers.json: expected a list"):
        load_seed(tmp_path)


def test_app_owns_its_copy_of_the_seed():
    """Changing the Seed after building the app doesn't change what the app serves."""
    seed = load_seed(DATA_DIR)
    client = TestClient(create_app(seed))
    seed.orders.clear()
    assert client.get("/orders/FG-1001").status_code == 200


def test_tiny_hand_made_seed():
    """Tests can build an app from a few hand-written records instead of the real files."""
    order = {
        "id": "FG-1", "customer_id": "C-1", "ordered_at": "2026-01-01",
        "delivered_at": "2026-01-03", "is_gift": False, "shipping_cost": "5.00",
        "items": [],
    }
    client = TestClient(create_app(build_seed([order], [], [])))
    assert client.get("/orders/FG-1").json()["shipping_cost"] == "5.00"
    assert client.get("/orders/FG-1001").status_code == 404


# -- GET /orders/{id}
def test_get_order(client):
    response = client.get("/orders/FG-1001")
    assert response.status_code == 200
    assert response.json() == {
        "id": "FG-1001",
        "customer_id": "CUST-002",
        "ordered_at": "2026-03-02",
        "delivered_at": "2026-03-05",
        "is_gift": False,
        "shipping_cost": "8.00",
        "items": [
            {
                "line_id": "1", "product_id": "P-SHIRT-LINEN", "name": "Linen button-up shirt",
                "size": "M", "color": "white", "price_paid": "68.00", "is_final_sale": False,
            }
        ],
    }


def test_order_with_two_items(client):
    items = client.get("/orders/FG-1004").json()["items"]
    assert [(i["line_id"], i["is_final_sale"]) for i in items] == [("1", True), ("2", False)]


def test_undelivered_order_has_null_delivery(client):
    assert client.get("/orders/FG-1009").json()["delivered_at"] is None


@pytest.mark.parametrize("order_id", ["FG-1010", "FG-1011"])
def test_corrupt_orders_served_as_is(client, order_id):
    """The API doesn't judge its data: catching these is the agent's job (cases #18, #19)."""
    assert client.get(f"/orders/{order_id}").status_code == 200


def test_unknown_order_404(client):
    response = client.get("/orders/FG-9999")
    assert response.status_code == 404
    assert error_code(response) == "order_not_found"


# -- GET /customers/{id}
@pytest.mark.parametrize(
    "customer_id, loyalty_since",
    [("CUST-001", "2024-06-01"), ("CUST-002", None)],
    ids=["member", "non-member"],
)
def test_get_customer(client, customer_id, loyalty_since):
    response = client.get(f"/customers/{customer_id}")
    assert response.status_code == 200
    assert response.json()["loyalty_since"] == loyalty_since


def test_unknown_customer_404(client):
    """CUST-999 is the dangling reference from FG-1010."""
    response = client.get("/customers/CUST-999")
    assert response.status_code == 404
    assert error_code(response) == "customer_not_found"


# -- GET /inventory/{product_id}?size=&color=
# A missing filter means "any". Each case lists the expected (size, color, in_stock) triples.
INVENTORY_CASES = [
    ("exact variant", "P-SHIRT-LINEN", {"size": "L", "color": "white"}, [("L", "white", True)]),
    ("quantity 0 is out of stock", "P-SHIRT-LINEN", {"size": "S", "color": "white"},
     [("S", "white", False)]),
    ("color only", "P-SHIRT-LINEN", {"color": "white"},
     [("S", "white", False), ("M", "white", True), ("L", "white", True)]),
    ("size only", "P-SHIRT-LINEN", {"size": "M"}, [("M", "white", True), ("M", "blue", True)]),
    ("no filters", "P-SHIRT-LINEN", {},
     [("S", "white", False), ("M", "white", True), ("L", "white", True), ("M", "blue", True)]),
    ("no size on product", "P-SCARF-STRIPED", {"color": "navy"}, [(None, "navy", False)]),
    ("no size or color", "P-LAMP-CERAMIC", {}, [(None, None, True)]),
    ("no match", "P-BOOTS-CHELSEA", {"size": "14"}, []),
]


@pytest.mark.parametrize(
    "product_id, params, expected",
    [case[1:] for case in INVENTORY_CASES],
    ids=[case[0] for case in INVENTORY_CASES],
)
def test_inventory_filters(client, product_id, params, expected):
    response = client.get(f"/inventory/{product_id}", params=params)
    assert response.status_code == 200
    body = response.json()
    assert body["product_id"] == product_id
    assert [(v["size"], v["color"], v["in_stock"]) for v in body["variants"]] == expected


def test_inventory_hides_quantity(client):
    """Storage has quantity; the API exposes only in_stock."""
    variant = client.get("/inventory/P-LAMP-CERAMIC").json()["variants"][0]
    assert set(variant) == {"size", "color", "in_stock"}


def test_unknown_product_404(client):
    """The discontinued tote from FG-1008 (case #16)."""
    response = client.get("/inventory/P-TOTE-CANVAS")
    assert response.status_code == 404
    assert error_code(response) == "product_not_found"
