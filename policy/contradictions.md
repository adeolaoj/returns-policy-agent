# Policy Contradictions & Resolutions

`returns_policy.md` was written by several teams and contains conflicts and gaps. This file
records each one, the decision made, and the reason. The engine cites these IDs (C1, C2, ...)
in its decision trace, so every decision can be traced back to a documented choice.

**General principle:** where the policy is ambiguous, choose the reading that (a) follows the
most specific section over the general summary, and (b) is clear enough that a customer and
an agent would reach the same answer. Where those don't settle it, it's a business call, and
it's marked as one.

---

## Precedence order

The engine evaluates rules in this order:

1. **Eligibility gates.** The item must pass all three:
   - **Final sale** (S4): blocks the request, *unless the item is defective* (C1).
   - **Condition** (S7): the item must be unused with tags, *unless it is defective* (S7's own exception).
   - **Return window**: the request date must be on or before the *latest* deadline among all
     windows that apply (C2).
2. **Exchange rules** (S9), for exchanges only: same product, different size or color, in stock.
3. **Refund method** (S6): gift purchases get store credit, everything else goes to the
   original payment method.
4. **Human approval** (S8): refunds over $500 are flagged for a person to approve.

If several gates fail, the engine reports all of them, not just the first, so the reply can
explain everything at once.

---

## Conflicts

### C1: Defective overrides final sale
- **Conflict:** S4 says clearance items "cannot be returned or exchanged." S5 says defective
  items can be returned "even if it was marked final sale."
- **Decision:** a defective item can be returned within 90 days even if it's final sale.
- **Why:** S5 is the more specific rule and names the exception explicitly. Selling a
  broken item and refusing to take it back is also the outcome a retailer least wants.

### C2: Overlapping windows: the customer gets the latest deadline
- **Conflict:** S1 (30 days), S2 (60 days, members), S3 (until Jan 31, holiday purchases), and
  S5 (90 days, defective) can all apply to the same item. S10 says members "keep their 60-day
  window" *and* the holiday extension "applies to everyone," but not which one wins.
- **Decision:** compute every deadline that applies and use the **latest** one.
- **Why:** this is the only reading where no rule ever *shortens* a window another rule
  granted. S10's wording ("keep", "applies to everyone") says both benefits stand, and taking
  the later deadline is how both stand at once. Example: a member's item bought Dec 20 and
  delivered Dec 22 gets Feb 20 (60 days), not Jan 31.

### C3: When does the clock start?
- **Conflict:** the summary says "within 30 days" (of what?). S1 says "of delivery." S3 is
  based on when the item was *bought*.
- **Decision:** S1, S2, and S5 count from the **delivery date**. S3 checks the **order date**
  (Nov 15 to Dec 31) and gives a fixed deadline of **Jan 31 of the following year**.
- **Why:** detailed sections take precedence over the summary. S3 says "bought" on purpose,
  since holiday shoppers buy early and delivery dates vary.

### C4: Membership is checked as of the order date
- **Gap:** S2 doesn't say *when* someone must be a member.
- **Decision:** the customer must have been a Rewards member **on the order date**.
- **Why (business call):** the benefit is tied to the purchase. Checking membership on the
  request date would let anyone join the day they want to return something and get a
  longer window. This is a single, clearly isolated check in the engine, so it's easy to
  change if the business decides otherwise.

### C5: "Unworn with tags" for items you can't wear
- **Gap:** S7 says "unworn, with tags," but Fernhill also sells home goods (lamps, throws,
  mugs).
- **Decision:** read it as **unused and in original condition, with tags or original packaging**.
  The engine takes a single yes/no input for this.
- **Why:** this applies the rule's intent (the item can be resold) to every category.

### C6: Gift purchases get store credit, even when defective
- **Gap:** S6 has no exception for defective gifts.
- **Decision:** gift purchases always get **store credit**, including defective items.
- **Why:** the person returning a gift usually isn't the person who paid for it, which is
  the reason S6 exists (the FAQ says the giver "isn't notified"). A defect doesn't change who
  paid.

### C7: Store credit counts toward the $500 approval threshold
- **Gap:** S8 says "refunds over $500." Is store credit a refund?
- **Decision:** yes. Any refund **over $500.00**, whether to the original payment method or as store
  credit, needs human approval. Exactly $500.00 does not.
- **Why:** store credit has the same cash value to the business, and Finance's concern
  (large payouts) applies equally to both. "Over" means strictly greater than.

### C8: Exchanges must pass the same eligibility checks as returns
- **Gap:** S9 doesn't say whether the window, final-sale, and condition rules apply to exchanges.
  S4 does say clearance items "cannot be returned *or exchanged*."
- **Decision:** an exchange must pass every eligibility gate a return would. Defective items
  can still be exchanged (C1 applies to exchanges too).
- **Why:** an exchange is a return paired with a new shipment. If exchanges skipped the
  checks, any return could be turned into an exchange to get around them.

### C9: What counts as a valid exchange
- **Gap:** S9 says "same item in a different size or color." Can both change? Can a broken
  item be swapped for an identical one?
- **Decision:** the new variant must be the **same product**, and **at least one** of size or
  color must differ (both may). An identical-variant swap isn't an exchange under S9. A
  customer with a defective item gets a refund and can reorder.
- **Why:** this follows the letter of S9. Allowing like-for-like replacements of defective
  items is a sensible v2 policy change (and a good test of the policy-versioning stretch goal).

### C10: Exchanges don't need $500 approval
- **Gap:** S8 is about refunds. A $600 coat exchanged for a different size moves no money.
- **Decision:** exchanges never trigger human approval.
- **Why:** S8 is about money going out. An exchange doesn't pay anything out.

### C11: Edge dates and amounts are inclusive
- **Decision:** day 30 (or 60, or 90) after delivery is still inside the window. Nov 15 and
  Dec 31 both count as holiday purchases. Jan 31 is the last valid day for holiday returns.
- **Why:** "within 30 days" is read inclusively, which also matches what a customer would expect.

### C12: Refund amount is the price paid for the item
- **Decision:** the refund is the item's **price paid** (after any sale discount). Shipping is
  not refunded (FAQ).
- **Why:** this is the most specific statement in the document.

---

## Assumptions and known limitations

- **Defect claims are taken as stated.** The engine trusts the `is_defective` input. A
  customer could falsely claim a defect to get around final sale. In production, defective
  returns would need a photo or inspection before the refund is issued. That's out of
  scope here, but the trace always shows when C1 was used, so these cases are easy to audit.
- **One item per request.** A customer could split a $900 refund into two $450 requests
  to avoid S8. An order-level running total would close that gap, but it's out of scope for v1.
- **The engine never reads the clock.** The request date is passed in. This keeps every
  decision reproducible and testable.
