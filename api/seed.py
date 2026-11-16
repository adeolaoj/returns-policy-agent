"""Load the seed data the mock backend serves.

The mock stands in for a backend we don't control, so records are kept exactly as stored,
with no business-rule checks (FG-1011, delivered before it was ordered, must still load).
The only checks are the loader's own integrity: each file is a list of records, every record
has an ID, and IDs are unique. The indexes are dicts, so a duplicate would silently replace the
first record. These fail at startup with a message that says where to look.
"""
import json
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Seed:
    """The backend's data, indexed by ID. Each value is the record exactly as stored."""
    orders: dict[str, dict]
    customers: dict[str, dict]
    inventory: dict[str, dict]  # keyed by product_id


def index_by(records: list[dict], key: str, kind: str) -> dict[str, dict]:
    """Build {record[key]: record}, raising ValueError on a missing or duplicate ID.

    e.g. index_by(orders, "id", "order") raises "duplicate order id 'FG-1001'".
    """
    result = {}
    for position, record in enumerate(records, start=1):
        # Check before reading, so the error names the record instead of a bare KeyError.
        if not isinstance(record, dict) or key not in record:
            raise ValueError(f"{kind} record #{position} has no {key!r}")
        record_id = record[key]
        if record_id in result:
            raise ValueError(f"duplicate {kind} id {record_id!r}")
        result[record_id] = record
    return result


def build_seed(orders: list[dict], customers: list[dict], inventory: list[dict]) -> Seed:
    """Index raw record lists into a Seed. Tests call this directly with tiny hand-made data."""
    indexed_orders = index_by(orders, "id", "order")
    indexed_customers = index_by(customers, "id", "customer")
    indexed_inventory = index_by(inventory, "product_id", "product")

    return Seed(
        orders=indexed_orders,
        customers=indexed_customers,
        inventory=indexed_inventory
    )


def read_records(path: Path) -> list:
    """Parse a JSON file whose top level must be a list of records."""
    data = json.loads(path.read_text(encoding="utf-8"))
    # Without this, a top-level object would loop over its keys (strings) and fail confusingly.
    if not isinstance(data, list):
        raise ValueError(f"{path.name}: expected a list of records at the top level")
    return data


def load_seed(directory: Path) -> Seed:
    """Read orders.json, customers.json and inventory.json from a directory."""
    orders = read_records(directory / "orders.json")
    customers = read_records(directory / "customers.json")
    inventory = read_records(directory / "inventory.json")

    return build_seed(orders, customers, inventory)
