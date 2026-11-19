from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.seed import load_seed

DATA_DIR = Path(__file__).parent.parent / "data"


@pytest.fixture
def client() -> TestClient:
    """A fresh app per test, so a refund in one test is invisible to the next."""
    return TestClient(create_app(load_seed(DATA_DIR)))


def post(client: TestClient, path: str, body: dict, key: str = "key-1"):
    """POST with an Idempotency-Key header."""
    return client.post(path, json=body, headers={"Idempotency-Key": key})


def error_code(response) -> str:
    return response.json()["detail"]["code"]


def in_stock(client: TestClient, product_id: str, size: str | None, color: str | None) -> bool:
    params = {k: v for k, v in {"size": size, "color": color}.items() if v is not None}
    [variant] = client.get(f"/inventory/{product_id}", params=params).json()["variants"]
    return variant["in_stock"]


# Valid bodies; tests copy one and change a field: {**REFUND, "amount": "999.00"}
REFUND = {"order_id": "FG-1001", "line_id": "1", "amount": "68.00",
          "method": "original_payment", "requires_approval": False}
EXCHANGE = {"order_id": "FG-1001", "line_id": "1", "size": "L", "color": "white"}
ESCALATION = {"reason": "data_problem", "order_id": "FG-1010",
              "context": "Order's customer CUST-999 doesn't exist."}


# -- POST /refunds
def test_refund_issued(client):
    response = post(client, "/refunds", REFUND)
    assert response.status_code == 201
    body = response.json()
    assert body["refund_id"].startswith("RF-")
    assert {k: body[k] for k in ("order_id", "line_id", "amount", "method", "status")} == {
        "order_id": "FG-1001", "line_id": "1", "amount": "68.00",
        "method": "original_payment", "status": "issued",
    }


def test_store_credit_is_a_refund_method(client):
    """C7: store credit is a refund paid to a different place, not a separate resource."""
    body = {**REFUND, "order_id": "FG-1006", "amount": "92.00", "method": "store_credit"}
    assert post(client, "/refunds", body).json()["method"] == "store_credit"


def test_refund_needing_approval_is_pending(client):
    """The $640 jacket: the refund exists from the start, waiting for a person (option B)."""
    body = {**REFUND, "order_id": "FG-1005", "amount": "640.00", "requires_approval": True}
    response = post(client, "/refunds", body)
    assert response.status_code == 201
    assert response.json()["status"] == "pending_approval"


@pytest.mark.parametrize(
    "change, code",
    [({"order_id": "FG-9999"}, "order_not_found"), ({"line_id": "9"}, "line_not_found")],
    ids=["order", "line"],
)
def test_refund_for_missing_order_or_line_404(client, change, code):
    response = post(client, "/refunds", {**REFUND, **change})
    assert response.status_code == 404
    assert error_code(response) == code


def test_refund_over_price_paid_rejected(client):
    response = post(client, "/refunds", {**REFUND, "amount": "68.01"})
    assert response.status_code == 422
    assert error_code(response) == "amount_exceeds_price_paid"


@pytest.mark.parametrize(
    "change",
    [{"method": "cash"}, {"amount": "-5.00"}, {"amount": "0"}, {"amount": "68.001"},
     {"surprise": True}],
    ids=["unknown method", "negative amount", "zero amount", "sub-cent amount", "extra field"],
)
def test_malformed_refund_rejected(client, change):
    """Requests are checked strictly: the API refuses rather than guessing what was meant."""
    assert post(client, "/refunds", {**REFUND, **change}).status_code == 422


def test_missing_idempotency_key_rejected(client):
    assert client.post("/refunds", json=REFUND).status_code == 422


# -- A line is resolved once (a backend invariant: the stateless engine can't know)
def test_line_refunded_only_once(client):
    """A second refund under a *new* key, e.g. a second chat about the same order."""
    assert post(client, "/refunds", REFUND, key="chat-1").status_code == 201
    response = post(client, "/refunds", REFUND, key="chat-2")
    assert response.status_code == 409
    assert error_code(response) == "line_already_resolved"


def test_exchanged_line_cannot_be_refunded(client):
    assert post(client, "/exchanges", EXCHANGE, key="a").status_code == 201
    response = post(client, "/refunds", REFUND, key="b")
    assert response.status_code == 409
    assert error_code(response) == "line_already_resolved"


def test_other_line_of_same_order_can_be_refunded(client):
    """Resolution is per line: refunding FG-1004's coat doesn't block its scarf."""
    coat = {**REFUND, "order_id": "FG-1004", "line_id": "2", "amount": "180.00"}
    scarf = {**REFUND, "order_id": "FG-1004", "line_id": "1", "amount": "24.00"}
    assert post(client, "/refunds", coat, key="a").status_code == 201
    assert post(client, "/refunds", scarf, key="b").status_code == 201


# -- Idempotency
def test_retry_replays_the_first_response(client):
    """The retry gets the same refund back. Had the server acted twice, it would be a 409."""
    first = post(client, "/refunds", REFUND)
    retry = post(client, "/refunds", REFUND)
    assert retry.status_code == 201
    assert retry.json() == first.json()


def test_same_meaning_is_the_same_request(client):
    """Bodies are compared parsed, not as bytes: key order and 68.0 vs. 68.00 don't matter."""
    first = post(client, "/refunds", REFUND)
    reordered = {**dict(reversed(list(REFUND.items()))), "amount": "68.0"}
    retry = post(client, "/refunds", reordered)
    assert retry.status_code == 201
    assert retry.json()["refund_id"] == first.json()["refund_id"]


def test_reused_key_with_different_body_rejected(client):
    post(client, "/refunds", REFUND)
    response = post(client, "/refunds", {**REFUND, "amount": "60.00"})
    assert response.status_code == 409
    assert error_code(response) == "idempotency_key_reused"


def test_keys_are_scoped_per_endpoint(client):
    """The same key on /refunds and /escalations names two different requests."""
    assert post(client, "/refunds", REFUND, key="k").status_code == 201
    assert post(client, "/escalations", ESCALATION, key="k").status_code == 201


def test_failures_are_not_stored(client):
    """Only successes are stored, so a fixed request can reuse the key of a failed one."""
    assert post(client, "/refunds", {**REFUND, "amount": "99.00"}).status_code == 422
    assert post(client, "/refunds", REFUND).status_code == 201


def test_exchange_retry_lowers_stock_once(client):
    """L/black jacket has 1 left. The retry replays instead of taking a second one."""
    body = {"order_id": "FG-1005", "line_id": "1", "size": "L", "color": "black"}
    first = post(client, "/exchanges", body)
    retry = post(client, "/exchanges", body)
    assert retry.json() == first.json()
    assert in_stock(client, "P-JACKET-LEATHER", "L", "black") is False


# -- POST /exchanges
def test_exchange_created(client):
    response = post(client, "/exchanges", EXCHANGE)
    assert response.status_code == 201
    body = response.json()
    assert body["exchange_id"].startswith("EX-")
    # The product comes from the line, so "same product" (C9) holds by construction.
    assert (body["product_id"], body["size"], body["color"], body["status"]) == (
        "P-SHIRT-LINEN", "L", "white", "created")


def test_exchange_lowers_stock(client):
    assert in_stock(client, "P-JACKET-LEATHER", "L", "black") is True
    post(client, "/exchanges", {"order_id": "FG-1005", "line_id": "1", "size": "L", "color": "black"})
    assert in_stock(client, "P-JACKET-LEATHER", "L", "black") is False


@pytest.mark.parametrize(
    "body, status, code",
    [
        ({**EXCHANGE, "size": "S"}, 409, "out_of_stock"),  # S/white has 0
        ({**EXCHANGE, "size": "XL"}, 404, "variant_not_found"),
        ({"order_id": "FG-1008", "line_id": "1", "size": None, "color": "black"},
         404, "product_not_found"),  # the discontinued tote
        ({**EXCHANGE, "line_id": "9"}, 404, "line_not_found"),
    ],
    ids=["out of stock", "no such variant", "discontinued product", "no such line"],
)
def test_exchange_refused(client, body, status, code):
    response = post(client, "/exchanges", body)
    assert response.status_code == status
    assert error_code(response) == code


def test_out_of_stock_exchange_leaves_line_unresolved(client):
    """A refused exchange changes nothing, so the customer can still take a return."""
    post(client, "/exchanges", {**EXCHANGE, "size": "S"}, key="a")
    assert post(client, "/refunds", REFUND, key="b").status_code == 201


# -- POST /escalations
def test_escalation_created(client):
    response = post(client, "/escalations", ESCALATION)
    assert response.status_code == 201
    body = response.json()
    assert body["ticket_id"].startswith("ESC-")
    assert (body["reason"], body["order_id"], body["status"]) == ("data_problem", "FG-1010", "pending")


def test_approval_escalation_points_to_pending_refund(client):
    """The $640 jacket takes two writes: the pending refund, then the escalation for it."""
    refund = {**REFUND, "order_id": "FG-1005", "amount": "640.00", "requires_approval": True}
    refund_id = post(client, "/refunds", refund, key="refund").json()["refund_id"]
    escalation = {"reason": "approval_required", "order_id": "FG-1005", "refund_id": refund_id,
                  "context": "Refund of $640.00 is over the $500.00 threshold (S8)."}
    response = post(client, "/escalations", escalation, key="escalation")
    assert response.status_code == 201
    assert response.json()["refund_id"] == refund_id


def test_escalation_without_order(client):
    """A service outage may happen before any order is known."""
    body = {"reason": "service_unavailable", "context": "Order lookup timed out twice."}
    response = post(client, "/escalations", body)
    assert response.status_code == 201
    assert response.json()["order_id"] is None


@pytest.mark.parametrize(
    "body",
    [
        {**ESCALATION, "reason": "bored"},
        {**ESCALATION, "context": ""},
        {"reason": "approval_required", "order_id": "FG-1005", "context": "Over $500."},
    ],
    ids=["unknown reason", "empty context", "approval without refund_id"],
)
def test_malformed_escalation_rejected(client, body):
    assert post(client, "/escalations", body).status_code == 422


@pytest.mark.parametrize(
    "change, code",
    [({"order_id": "FG-9999"}, "order_not_found"), ({"refund_id": "RF-9999"}, "refund_not_found")],
    ids=["order", "refund"],
)
def test_escalation_with_unknown_reference_404(client, change, code):
    response = post(client, "/escalations", {**ESCALATION, **change})
    assert response.status_code == 404
    assert error_code(response) == code


# -- Isolation
def test_writes_dont_leak_between_apps():
    """Two apps from the same Seed each own their state (the deepcopy in create_app)."""
    seed = load_seed(DATA_DIR)
    first, second = TestClient(create_app(seed)), TestClient(create_app(seed))
    assert post(first, "/refunds", REFUND).status_code == 201
    assert post(second, "/refunds", REFUND).status_code == 201
