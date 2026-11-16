# Seed data cases

The mock backend serves `orders.json`, `customers.json` and `inventory.json`. Each case below
is one scenario where the **agent** does something different: a different reply, API write, or
route through the graph. Rule edges (the $500.00 vs. $500.01 boundary, day 30 vs. day 31, the holiday dates)
are covered by the engine tests and don't get seed records.

**Pinned date:** every case assumes `today = 2026-03-16`. The agent never reads the clock in
evals; "today" is injected, so these cases don't drift as real time passes.

**What lives where:** the data holds what the business knows (orders, customers, stock). The
customer's claim (defective? unused?) and the variant they want come from the conversation,
so one order can serve several scenarios.

## Scenarios

| # | Order | Data | Customer says | Expected agent behavior |
|---|---|---|---|---|
| 1 | FG-1001 | Shirt, $68, delivered Mar 5, $8 shipping | Unused, wants a refund | Refund $68.00 to original payment. Shipping isn't refunded (C12). |
| 2 | FG-1001 | same | Wore it, not defective | Deny (S7 condition). |
| 3 | FG-1001 | M/white → L/white has 6 in stock | Exchange for a large | Exchange approved. |
| 4 | FG-1001 | S/white has 0 in stock | Exchange for a small | Deny (out of stock, C9). Reply offers a return instead. |
| 5 | FG-1002 | Boots, delivered Jan 24 (day 51), non-member | Unused, wants a refund | Deny (past the 30-day window). |
| 6 | FG-1003 | Same dates as FG-1002, member since 2024 | Unused, wants a refund | Refund $150.00 (60-day loyalty window, C2). The agent must look up the customer to get this right. |
| 7 | FG-1004 | Two items: final-sale scarf + coat | "Return my order" (no item named) | Ask which item. Don't guess. |
| 8 | FG-1004 | Line 1, final-sale scarf | Unused, wants a refund | Deny (S4 final sale). |
| 9 | FG-1004 | Line 1, final-sale scarf | It's defective | Refund $24.00 (C1). Trace flags the defect as unverified. |
| 10 | FG-1004 | Line 2, coat | Unused, wants a refund | Refund $180.00, from the right line item. |
| 11 | FG-1005 | Jacket, $640 | Unused, wants a refund | Refund needs human approval (S8). Escalation with `reason="approval_required"`. Reply says it's pending. |
| 12 | FG-1005 | M/black → L/black has 1 in stock | Exchange for a large | Exchange, no approval needed (C10). |
| 13 | FG-1006 | Gift order, sweater $92 | Unused, wants a refund | Store credit $92.00 (S6). |
| 14 | FG-1007 | Lamp, no size or color | Wants to swap for another lamp | Deny (nothing to change, C9). Reply offers a return. |
| 15 | FG-1008 | Tote, product no longer in inventory | Unused, wants a refund | Refund $45.00. Returns don't need inventory. |
| 16 | FG-1008 | same | Wants a different color | Inventory has no such product. Reply explains it's discontinued and offers a return. |
| 17 | FG-1009 | `delivered_at: null` | Wants to return it | "Hasn't arrived yet" reply. The engine is never called. |
| 18 | FG-1010 | `customer_id` CUST-999 doesn't exist | Wants a refund | Escalate as a data problem. The customer gets a generic "we're looking into it" reply. |
| 19 | FG-1011 | Delivered before it was ordered | Wants a refund | `OrderFact` validation fails at the boundary. Escalate like #18. |
| 20 | FG-9999 | No such order | Wants a refund | 404. Ask the customer to check the order number. |

### Fault IDs (no records, handled in code in step 4)

| Order | Behavior | Tests |
|---|---|---|
| FG-ERR-500 | Always returns 500 | Retry, then escalate |
| FG-TIMEOUT | Sleeps past the client timeout | Same path, different failure |
| FG-FLAKY | Fails the first call, succeeds on the second | The retry actually works |

## Data format decisions

- **Money is a string** (`"68.00"`). JSON numbers load as Python floats, which can't hold
  68.10 exactly. Pydantic turns the string into a `Decimal` with no rounding.
- **Each file is a list, not a dict keyed by ID.** JSON objects have the same silent
  duplicate-key problem as YAML. The loader builds the index and rejects duplicate IDs.
- **`is_final_sale` and `price_paid` are on the line item.** They describe the item as sold.
  **`is_gift` and `shipping_cost` are on the order.** A gift is how the whole order was
  bought, and that's the reason behind C6 (who paid).
- **Inventory stores `quantity`, not `in_stock`.** The API answers `in_stock = quantity > 0`,
  and an exchange write can decrement it.
- **Order IDs are opaque** (`FG-1001`), so the ID doesn't hint the outcome to the LLM. This
  file is the map from ID to purpose. Fault IDs are descriptive because they're code triggers.
