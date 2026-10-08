# Campus Customs — Agent Harness

Operating manual for the Campus Customs multi-agent desk (MGT 409, Homework 5).

- **Live database:** `data/campus_customs_new.db` — the working copy every agent reads and writes.
- **Pristine seed:** `data/campus_customs.db` — never written to; the reset source of truth.
- **Engine:** SQLite 3, 9 tables, no views and no triggers.

---

## 1. The Desk at a Glance

Everything the team decides is anchored to one reference date and one cash number. Agents must read both from the database, never assume them.

| Fact | Value | Source | Why it drives behavior |
| --- | --- | --- | --- |
| Today | `2026-08-31` | `desk.date_today` | The only clock the agents may use — "overdue" and "due soon" mean nothing without it. Do **not** call `datetime.now()`. |
| Checking balance | `$3,400.00` | `cash_accounts.balance` | The hard ceiling on everything Accounting and Facilities can disburse. |
| Invoice 501 | `$840.00`, due `2026-08-28`, **open** | `invoices` | Already **3 days overdue** as of the desk date — the most urgent money item on the board. |
| Lease 1 rent | `$2,400.00`, next due `2026-09-02` | `leases` | Due in **2 days** — Facilities has to queue it before it goes late. |
| Open tickets | 101, 102, 103 | `tickets` | The entire workload. All three are `status = 'open'`. |
| Payments ledger | **empty** (0 rows) | `payments` | Nothing has been disbursed yet; every outflow the team makes must land here. |

**Cash arithmetic the team must reproduce exactly:**

```
 3,400.00   starting checking balance
  -840.00   ticket 101 -> invoice 501, Bulldog Print Co rush reprint
=2,560.00
-2,400.00   ticket 102 -> lease 1, Elm City Properties Chapel Street rent
=  160.00   ending balance
```

Ticket 103 is a **quote**, not a disbursement — it moves zero cash. Any other cash movement is a hallucination.

---

## 2. Table Reference

Nine tables. For each: every column, and what breaks if an agent ignores it.

### 2.1 `desk` — the clock

| Column | Type | Null | Key |
| --- | --- | --- | --- |
| `date_today` | TEXT | NOT NULL | — |
| `notes` | TEXT | nullable | — |

Single-row table holding `2026-08-31`. **Why it matters:** this is the team's shared "now." Every overdue check, due-in-N-days check, and `paid_at` stamp has to resolve against this value, or Accounting and Facilities will disagree about what is late. One row, no primary key — agents read it, never insert into it.

**Workflow tie:** `desk.date_today = '2026-08-31'` is the reference date for every overdue check on this board. Measured against it, invoice 501 ($840.00, due 2026-08-28) is **3 days overdue**, and lease 1 rent ($2,400.00, due 2026-09-02) is **due in 2 days**. Both verdicts flip if an agent substitutes the system clock.

### 2.2 `cash_accounts` — the wallet

| Column | Type | Null | Key |
| --- | --- | --- | --- |
| `name` | TEXT | nullable\* | **PK** |
| `balance` | REAL | NOT NULL | — |
| `date` | TEXT | NOT NULL | — |

One account: `checking`, `$3,400.00`, as of `2026-08-31`. **Why it matters:** the single authoritative balance. Accounting must debit it on every approved payment and the dashboard reads it live — if an agent writes a payment row without decrementing this balance, the books silently break. Check it *before* approving a disbursement; $3,400 covers both outflows, but only in that order.

\* SQLite exempts TEXT primary keys from implicit NOT NULL, so `name` is technically nullable. Treat it as required.

**Workflow tie:** the checking balance **starts at $3,400.00**. Ticket 101 takes it to $2,560.00 (invoice 501, $840.00), ticket 102 takes it to $160.00 (lease 1 rent, $2,400.00), and ticket 103 leaves it untouched because a quote moves no cash. $160.00 is the only correct ending balance.

### 2.3 `tickets` — the workload

| Column | Type | Null | Key |
| --- | --- | --- | --- |
| `id` | INTEGER | nullable\* | **PK** |
| `type` | TEXT | NOT NULL | — |
| `requester` | TEXT | NOT NULL | — |
| `subject` | TEXT | NOT NULL | — |
| `sku` | TEXT | nullable | → `inventory.sku` / `pricing.sku` (undeclared) |
| `size` | TEXT | nullable | → `inventory.size` (undeclared) |
| `qty` | INTEGER | nullable | — |
| `lease_id` | INTEGER | nullable | **FK** → `leases.id` |
| `invoice_id` | INTEGER | nullable | **FK** → `invoices.id` |
| `status` | TEXT | NOT NULL | — |
| `notes` | TEXT | nullable | — |
| `created_at` | TEXT | NOT NULL | — |

Three open rows — 101, 102, 103. **Why it matters:** `type` is the routing key the Boss agent dispatches on (`customer_order` → Inventory / Customer Service, `rent_notice` → Facilities, `price_override` → Accounting), and the nullable foreign keys tell each specialist exactly which other table to open. `status` is the completion signal the dashboard renders. Note the sparse-column design: which columns are populated *is* the ticket's shape.

**Workflow tie:** the three open tickets map one-to-one onto the obligations above — 101 carries `invoice_id=501` (the $840.00 overdue bill), 102 carries `lease_id=1` (the $2,400.00 rent due 2026-09-02), and 103 carries only `sku`/`size`/`qty`, which is how an agent knows it is a quote with no payment attached.

### 2.4 `inventory` — what is on the shelf

| Column | Type | Null | Key |
| --- | --- | --- | --- |
| `sku` | TEXT | NOT NULL | **PK (1/2)** |
| `name` | TEXT | NOT NULL | — |
| `size` | TEXT | NOT NULL | **PK (2/2)** |
| `qty` | INTEGER | NOT NULL | — |
| `location` | TEXT | NOT NULL | — |

10 rows across 4 SKUs. **Why it matters:** the composite `(sku, size)` key is the whole point — stock is per size, so "do we have hoodies?" is never a valid question. Two rows sit at zero (`CC-TEE-WHITE / S` and `CC-MUG-CREST / OS`), and one of those zeros is the reason ticket 101 exists. `location` is what Customer Service quotes to a walk-in.

| SKU | Name | S | M | L | XL | OS | Location |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `CC-HOOD-NAVY` | Basic Hoodie Big Yale | 4 | **8** | 14 | 6 | — | Aisle A |
| `CC-TEE-WHITE` | Classic Bulldog Tee | **0** | 5 | 3 | 2 | — | Aisle B |
| `CC-HAT-BLUE` | Yale Cap | — | — | — | — | 40 | Aisle C |
| `CC-MUG-CREST` | Crest Mug | — | — | — | — | **0** | Aisle D |

**Workflow tie:** both zero rows are live workflow blockers. `CC-TEE-WHITE / S` at 0 is *why* ticket 101 exists and why the overdue $840.00 reprint invoice has to clear. `CC-HOOD-NAVY / M` at 8 against ticket 103's ask of 20 is a 12-unit shortfall that no discount fixes.

### 2.5 `pricing` — the margin floor

| Column | Type | Null | Key |
| --- | --- | --- | --- |
| `sku` | TEXT | nullable\* | **PK** |
| `unit_cost` | REAL | NOT NULL | — |
| `list_price` | REAL | NOT NULL | — |

| SKU | Unit cost | List price | Gross margin |
| --- | --- | --- | --- |
| `CC-HOOD-NAVY` | $22.00 | $58.00 | 62.1% |
| `CC-TEE-WHITE` | $8.00 | $28.00 | 71.4% |
| `CC-HAT-BLUE` | $6.00 | $22.00 | 72.7% |
| `CC-MUG-CREST` | $3.50 | $14.00 | 75.0% |

**Why it matters:** this is the only table that can justify or kill a discount. Priced per SKU, *not* per size, so a bulk quote multiplies one `list_price` by quantity. `unit_cost` is the floor — any discount an agent grants has to be checked against it, or the team cheerfully sells below cost. Ticket 103 lives or dies here.

**Workflow tie:** ticket 103 is priced entirely from this table — 20 × $58.00 = $1,160.00 at list against 20 × $22.00 = $440.00 of cost. The quote is cash-neutral, so the $160.00 ending balance stands regardless of the discount granted.

### 2.6 `invoices` — money owed out

| Column | Type | Null | Key |
| --- | --- | --- | --- |
| `id` | INTEGER | nullable\* | **PK** |
| `vendor_id` | INTEGER | NOT NULL | **FK** → `vendors.id` |
| `amount` | REAL | NOT NULL | — |
| `due_date` | TEXT | NOT NULL | — |
| `status` | TEXT | NOT NULL | — |
| `description` | TEXT | nullable | — |

One row: invoice 501, vendor 1, $840.00, due 2026-08-28, `open`, "Rush reprint CC-TEE-WHITE S". **Why it matters:** `due_date` compared against `desk.date_today` is how Accounting discovers the invoice is already 3 days late, and `status` must flip `open → paid` when the disbursement clears. The `description` is the tell that this invoice *is* the fix for ticket 101's stockout — same SKU, same size.

**Workflow tie:** **invoice 501 is overdue — $840.00, due 2026-08-28, 3 days past the 2026-08-31 reference date.** It is the first outflow on the board: paying it drops checking from $3,400.00 to $2,560.00, flips `status` to `paid`, and unblocks ticket 101.

### 2.7 `leases` — recurring obligations

| Column | Type | Null | Key |
| --- | --- | --- | --- |
| `id` | INTEGER | nullable\* | **PK** |
| `space_name` | TEXT | NOT NULL | — |
| `landlord` | TEXT | NOT NULL | — |
| `monthly_rent` | REAL | NOT NULL | — |
| `next_due` | TEXT | NOT NULL | — |
| `notes` | TEXT | nullable | — |

One row: lease 1, Chapel Street shop, Elm City Properties, $2,400.00/mo, next due 2026-09-02. **Why it matters:** rent is the largest single outflow on the board and the one that is *not* yet late — Facilities has a 2-day window to act. `monthly_rent` is the authoritative amount; the email in ticket 102 is a claim, the lease row is the fact, and the agent must pay the lease, not the email.

**Workflow tie:** **lease 1 rent is due in 2 days — $2,400.00, due 2026-09-02 against the 2026-08-31 reference date.** It is the second outflow: paid after invoice 501, it takes checking from $2,560.00 to $160.00. Paid *before* it, the $840.00 invoice still clears — but the team must never queue a third disbursement, because $160.00 covers nothing.

### 2.8 `vendors` — who fills the gaps

| Column | Type | Null | Key |
| --- | --- | --- | --- |
| `id` | INTEGER | nullable\* | **PK** |
| `name` | TEXT | NOT NULL | — |
| `specialty` | TEXT | NOT NULL | — |
| `lead_days` | INTEGER | NOT NULL | — |

| ID | Name | Specialty | Lead days |
| --- | --- | --- | --- |
| 1 | Bulldog Print Co | apparel reprint | 5 |
| 2 | Elm City Gifts | mugs and small goods | 3 |
| 3 | QuickShip CT | local courier | 1 |

**Why it matters:** `lead_days` converts a stockout into a date a customer can be told. Bulldog Print Co's 5 days against the desk date puts the ticket-101 tee reprint at roughly **2026-09-05** — that is the promise Customer Service makes. `specialty` is how an agent picks the right vendor instead of guessing.

**Workflow tie:** vendor 1 (Bulldog Print Co) sits behind both open stock problems. 5 lead days from the 2026-08-31 reference date puts ticket 101's tee reprint at roughly **2026-09-05**, and the same lead time governs the 12 missing size-M hoodies in ticket 103.

### 2.9 `payments` — the audit ledger

| Column | Type | Null | Key |
| --- | --- | --- | --- |
| `id` | INTEGER | nullable\* | **PK** |
| `kind` | TEXT | NOT NULL | — |
| `ref_id` | INTEGER | nullable | → `invoices.id` *or* `leases.id` (undeclared, polymorphic) |
| `amount` | REAL | NOT NULL | — |
| `account` | TEXT | NOT NULL | → `cash_accounts.name` (undeclared) |
| `paid_at` | TEXT | NOT NULL | — |
| `approved_by` | TEXT | NOT NULL | — |

**Currently empty.** **Why it matters:** this is where the team proves what it did. Every approved disbursement writes exactly one row, and the two rows it should end with ($840 invoice, $2,400 rent) must reconcile to the $160 ending balance. `approved_by` is the human-in-the-loop record — an agent must never self-approve a payment — and `ref_id` is polymorphic, so `kind` is what tells you whether `ref_id` points at an invoice or a lease.

**Workflow tie:** this ledger should end with **exactly two rows** — $840.00 against invoice 501 and $2,400.00 against lease 1, both on account `checking`, both stamped against the 2026-08-31 desk date and signed by a human in `approved_by`. Sum them against the $3,400.00 opening balance and you must land on $160.00; a third row means an agent invented a payment.

---

## 3. The Three Open Tickets

### Ticket 101 — `customer_order` · Tauhid Zaman · "Bulldog tee"

`sku=CC-TEE-WHITE`, `size=S`, `qty=1`, `invoice_id=501`, created 2026-08-31T09:15.

The customer wants one small Bulldog tee. `inventory(CC-TEE-WHITE, S).qty = 0` — **the shelf is empty**. The ticket's own `invoice_id` points at invoice 501, the $840 rush reprint of exactly that SKU and size from Bulldog Print Co, and that invoice is **3 days overdue**. So the chain is: stockout → the reprint that fixes it is already ordered → but the vendor has not been paid. Pay the $840, then quote the customer the vendor's 5-day lead time (~2026-09-05).

**Links:** `tickets.sku` / `tickets.size` → `inventory` (stock check) and `pricing` (the $28.00 list price) · `tickets.invoice_id` → `invoices.501` → `vendors.1`.

### Ticket 102 — `rent_notice` · Elm City Properties · "Rent due"

`lease_id=1`, no SKU, created 2026-08-31T11:05.

An email says shop rent is due in 2 days. Lease 1 confirms it: $2,400.00 to Elm City Properties for the Chapel Street shop, due 2026-09-02. Verify the email's claim against the lease row, then queue the payment — after the $840 invoice, $2,560 remains, which covers it with $160 to spare. Order matters: both outflows fit, but only if nothing else is invented.

**Links:** `tickets.lease_id` → `leases.1` · payment lands in `payments` (`kind='rent'`, `ref_id=1`) and debits `cash_accounts.checking`.

### Ticket 103 — `price_override` · Yale AI Club · "Bulk hoodie discount"

`sku=CC-HOOD-NAVY`, `size=M`, `qty=20`, created 2026-08-31T16:55.

A student org wants 20 navy hoodies in size M at a discount. Two independent problems, and an agent that only looks at one will get this wrong:

1. **Margin.** List $58.00 against unit cost $22.00 → $1,160.00 at list, $440.00 of cost, $720.00 of gross margin. There is real room to discount, but $22.00/unit is the floor.
2. **Stock.** `inventory(CC-HOOD-NAVY, M).qty = 8` — the shop is **12 units short**. Other sizes (4 S / 14 L / 6 XL) do not substitute for a size-M order.

This is a **quote**, not a sale: zero cash moves, the checking balance stays at $160.00, and any fulfillment date depends on a Bulldog Print Co reprint at 5 days' lead time.

**Links:** `tickets.sku` → `pricing` (discount math) and `inventory` (the shortfall) · `vendors.1` for the lead time on the missing 12.

---

## 4. How the Tables Connect

```
         desk (2026-08-31)   <- the clock every check resolves against
                 |
   +-------------+-------------+------------------+
tickets.101   tickets.102   tickets.103
   | sku/size      | lease_id    | sku
   | invoice_id    v             +--> pricing    (unit_cost / list_price)
   |            leases.1         +--> inventory  (qty by size)
   v             $2,400
invoices.501 ---> vendors.1 (lead_days 5)
   $840                  |
   +----------+----------+
              v
          payments  --debits-->  cash_accounts.checking  (3,400 -> 160)
```

**Declared foreign keys:** `invoices.vendor_id → vendors.id`, `tickets.lease_id → leases.id`, `tickets.invoice_id → invoices.id`. Everything else is a convention the agents must honor manually.

---

## 5. Integrity Notes for Tool Authors

Real constraints of this schema — read before writing MCP tools against it.

1. **Foreign keys are declared but not enforced.** `PRAGMA foreign_keys` defaults to `0` in SQLite. Issue `PRAGMA foreign_keys = ON` on every connection, or a bad `ref_id` writes silently.
2. **Three relationships are undeclared.** `tickets.sku/size → inventory`, `payments.account → cash_accounts.name`, and `payments.ref_id → invoices.id | leases.id` have no FK at all. `payments.ref_id` is polymorphic — always read it together with `kind`.
3. **Inventory is keyed on `(sku, size)`.** Any stock tool that filters on `sku` alone returns four rows and overstates availability.
4. **Pricing is keyed on `sku` only.** No per-size prices; do not try to join pricing on size.
5. **`desk` and `cash_accounts` are singletons.** One row each. Tools should read them with `LIMIT 1` and update rather than insert.
6. **INTEGER PRIMARY KEYs are rowid aliases**, so they report as nullable in `PRAGMA table_info` and auto-assign on insert — let `payments.id` assign itself.
7. **Money is `REAL`.** Floating point. Round to 2 decimals at every boundary (tool output, ledger write, balance update) so the $160.00 reconciliation lands exactly.
8. **Writes go to `campus_customs_new.db` only.** `campus_customs.db` stays pristine so Problem 9 can reset to the $3,400.00 starting state.
9. `PRAGMA integrity_check` on the working copy returns `ok`, and the two files are byte-identical at this point (same MD5).

---

## 6. MCP Tools

Server: `mcp_server/server.py` (FastMCP, name `campus-customs-ops`), bound to
`data/campus_customs_new.db` and opened **read-only** (`mode=ro`) — these three tools
physically cannot write to the desk. Every value is a `SELECT` result; a missing row
returns `found=False` with the valid options rather than a guess, and every date
judgement resolves against `desk.date_today`, never the wall clock.

Tools are named `get_<what it returns>` and each returns a declared Pydantic model, so
the field names, types, and per-field descriptions are visible in the MCP schema before
an agent makes its first call.

| Tool | Reads | Unlocks |
| --- | --- | --- |
| `get_vendor_invoices(status, sku)` → `VendorInvoiceReport` | `invoices`, `vendors`, `tickets`, `inventory`, `desk`, `cash_accounts` | **Ticket 101** |
| `get_lease_details(lease_id, due_within_days)` → `LeaseDetailReport` | `leases`, `tickets`, `desk`, `cash_accounts` | **Ticket 102** |
| `get_inventory_and_pricing(sku, size, qty)` → `InventoryPricingReport` | `inventory`, `pricing`, `desk` | **Ticket 103** |

### 6.1 `get_vendor_invoices` → ticket 101

**The constraint:** ticket 101 asks for one `CC-TEE-WHITE / S`, and that shelf is at
**qty 0**. The ticket carries `invoice_id=501`, so the reprint that would refill it has
already been ordered from Bulldog Print Co — but invoice 501 is **$840.00, due
2026-08-28, unpaid**, which is 3 days before the desk's 2026-08-31 reference date.
Nothing ships until that bill clears.

**Why this is the right tool:** every other table says the shop is simply out of tees;
only the invoice row explains *why the restock is stalled*, and this is the tool that
reads it. It returns `days_past_due: 3` and `is_overdue: true` computed against
`desk.date_today` rather than the wall clock, carries the linked ticket with
`qty_on_hand: 0` so the stockout and the unpaid bill arrive in one response, converts
the vendor's `lead_days: 5` into `earliest_delivery_if_paid_today: 2026-09-05`, and
includes the $3,400.00 checking balance so Accounting can confirm the $840.00 is
affordable without a second call.

**The decision it enables:** pay invoice 501 now (balance $3,400.00 → $2,560.00), then
tell Tauhid Zaman 2026-09-05 — a date from `vendors.lead_days`, not an invented promise.

### 6.2 `get_lease_details` → ticket 102

**The constraint:** ticket 102 is an *email* — `notes: "Email: shop rent due in 2 days."`
An agent that pays what a message asserts is one spoofed email away from wiring money to
the wrong landlord. The authoritative figures are lease 1: **$2,400.00 to Elm City
Properties for the Chapel Street shop, due 2026-09-02**, two days after the reference
date.

**Why this is the right tool:** it is the only way to replace the claim with the record.
It returns the lease row — landlord, space, `monthly_rent`, `next_due` — plus
`days_until_due: 2` measured from `desk.date_today`, and `covered_by_checking` so
Facilities sees affordability against the live balance. The ticket is attached under
`linked_tickets` with its `notes` preserved, which lets the agent compare claim against
record instead of choosing between them. A `lease_id` that does not exist returns
`found=False` and `available_lease_ids: [1]` — it will not invent a rent amount for a
lease that was never signed.

**The decision it enables:** pay $2,400.00 against lease 1 (balance $2,560.00 →
$160.00), with two days of margin before it goes late — and pay the lease's amount,
not the email's.

### 6.3 `get_inventory_and_pricing` → ticket 103

**The constraint:** the Yale AI Club wants **20 `CC-HOOD-NAVY` in size M at a discount**,
and the request fails two different tests at once. Size M holds **8 units** — but the SKU
totals 32 across all sizes, so any agent that checks the SKU rather than the `(sku, size)`
pair concludes the order is fine and is wrong by 12 units. And a discount quoted without
`unit_cost` ($22.00 under a $58.00 list) can price below cost while looking generous.

**Why this is the right tool:** it answers both halves in one call, and its shape makes
the trap hard to fall into. `stock_by_size` is returned per size with
`total_units_all_sizes` explicitly documented as *not* a measure of one size's
availability; passing `size="M", qty=20` yields `shortfall: 12` and
`can_fulfill_from_stock: false` directly, so the gap is stated rather than inferred.
On the money side it returns `price_floor_per_unit: 22.00` and
`max_discount_pct_before_selling_at_cost: 62.1`, bounding the discount with a number
from `pricing` instead of a negotiating instinct. The `quote` block is labelled as
moving no cash, which is what keeps the $160.00 ending balance intact.

**The decision it enables:** quote 8 units now and 12 on a Bulldog Print Co reprint, at
a discount no deeper than 62.1% off list — and book no cash movement, because a quote is
not a sale.

### Returned shape

Each response carries `as_of` (the reference date the answer was computed against) and
`source_tables` (what it read), so the audit trail can show where any claim came from.
Amounts are rounded to 2 decimals on the way out, because `REAL` columns otherwise drift
the $3,400.00 → $160.00 reconciliation.

### Verified against the seeded desk

| Call | Result |
| --- | --- |
| `get_vendor_invoices()` | invoice 501, $840.00, `days_past_due: 3`, `is_overdue: true`, vendor Bulldog Print Co (5 lead days), `earliest_delivery_if_paid_today: 2026-09-05`, linked ticket 101 with `qty_on_hand: 0` |
| `get_lease_details()` | lease 1, Chapel Street shop, $2,400.00, `days_until_due: 2`, `covered_by_checking: true`, linked ticket 102 |
| `get_inventory_and_pricing("CC-HOOD-NAVY", "M", 20)` | 8 on hand, `shortfall: 12`, `can_fulfill_from_stock: false`, $1,160.00 at list, $440.00 cost, $720.00 margin, floor $22.00/unit, max discount 62.1% |
| `get_inventory_and_pricing("CC-NOPE")` | `found: false` plus the four real SKUs — no invented product |
| `get_lease_details(99)` | `found: false` plus `available_lease_ids: [1]` |
| `UPDATE` through the tool connection | `attempt to write a readonly database` |

---

## 7. Registering and Smoke-Testing the Server

`.mcp.json` at the project root registers the server as **`campus-customs-ops`** alongside
the two servers already configured for this repo (`yale-som-courses`, `outlook-mail`):

```json
"campus-customs-ops": {
  "command": "<project>/mcp_servers/.venv/Scripts/python.exe",
  "args":    ["<project>/mcp_server/server.py"],
  "env":     { "CAMPUS_CUSTOMS_DB": "<project>/data/campus_customs_new.db" }
}
```

Three notes on that config:

- **The interpreter is the venv, not the machine Python.** The machine-wide install fails
  on `import FastMCP` (`ModuleNotFoundError: uncalled_for`); `mcp_servers/.venv/` carries a
  working FastMCP 4.0.10. Pointing `command` anywhere else makes the server fail to start.
- **`CAMPUS_CUSTOMS_DB` is set explicitly** even though `server.py` already resolves the
  working copy relative to its own location — so the database a client is talking to is
  visible in the config rather than implied by the code.
- **Paths are absolute** because an MCP client's working directory is not guaranteed to be
  the project root.

### Smoke test

`output/mcp_smoke.json` records one call per tool, launched over stdio **from the
`.mcp.json` entry itself** rather than by importing the module, so the test exercises the
registered configuration. Each record holds the `prompt` asked, the `tool_name`, the
`tool_args` sent, and the verbatim `tool_output` returned over the protocol.

Every figure was then compared against a direct SQL read of the same database — 12 checks,
all passing:

| Checked | Tool returned | Database holds |
| --- | --- | --- |
| Invoice 501 amount | `840.0` | `840.0` |
| Invoice 501 due date | `2026-08-28` | `2026-08-28` |
| Invoice 501 vendor | `Bulldog Print Co` | `Bulldog Print Co` |
| `CC-TEE-WHITE / S` on hand (ticket 101) | `0` | `0` |
| Lease 1 monthly rent | `2400.0` | `2400.0` |
| Lease 1 next due | `2026-09-02` | `2026-09-02` |
| Lease 1 landlord | `Elm City Properties` | `Elm City Properties` |
| `CC-HOOD-NAVY` list price | `58.0` | `58.0` |
| `CC-HOOD-NAVY` unit cost | `22.0` | `22.0` |
| `CC-HOOD-NAVY / M` on hand | `8` | `8` |
| Checking balance | `3400.0` | `3400.0` |
| Desk reference date | `2026-08-31` | `2026-08-31` |

The derived figures in those same responses follow from the verified inputs:
`days_past_due: 3` and `days_until_due: 2` against the 2026-08-31 reference date,
`earliest_delivery_if_paid_today: 2026-09-05` from 5 vendor lead days, and
`shortfall: 12` from 20 requested against 8 on hand.

---

## 8. The Agent Team

Five PydanticAI agents, all running **`gpt-6-luna`** through the Portkey gateway with
`PORTKEY_API_KEY`. The model name is a constant in `backend/agents.py`, not an env lookup —
and if `PORTKEY_MODEL` is set to anything else the team refuses to start rather than
silently running a different model.

Each agent's instructions come from its own file in `backend/prompts/`, and each returns a
typed `AgentOutput` (summary, cited facts, recommended actions, prepared vouchers, blocking
issues, `needs_human`, proposed ticket status) rather than free text.

| Agent | Prompt file | Owns | Typically called for |
| --- | --- | --- | --- |
| **Boss** | `boss.md` | Reading the board, routing, assembling the answer | Every ticket — entry point |
| **Inventory** | `inventory.md` | Stock by `(sku, size)`, shortfalls, restock dates | 101, 103 |
| **Accounting** | `accounting.md` | Invoices, overdue bills, cash, margin, vouchers | 101, 103 |
| **Facilities** | `facilities.md` | The lease, rent, the landlord | 102 |
| **Customer Service** | `customer_service.md` | What the requester is actually told | 101, 103 |

### Delegation

The Boss routes on what the ticket carries — `invoice_id` → Accounting, `lease_id` →
Facilities, `sku`/`size`/`qty` → Inventory, a waiting customer → Customer Service — and the
prompts explicitly reject "Boss calls everyone" as a failure mode rather than thoroughness.

**Specialists also delegate to each other.** Every agent is exposed to the others as an
`ask_<agent>` tool, so Accounting can ask Inventory about stock without relaying through
the Boss. Each hand-off spends one shared turn from the ticket's budget, and is written to
the audit trail with the question that was asked.

### Execution loop

`backend/orchestrator.py` → `run_ticket(ticket_id)`:

1. Append `run_started`.
2. Read the ticket from MCP (`get_open_tickets`) — the run starts from recorded fact.
3. Run the Boss with the ticket's facts; it delegates as needed.
4. Every delegation and every MCP tool call is appended as it happens.
5. Append `run_finished` and return a typed `TicketRun`.

`backend/` never opens SQLite. Every shop fact — including the orchestrator's own ticket
lookup — travels through the MCP server. There is no second database layer.

---

## 9. MCP Tools (complete list)

Eight tools on `campus-customs-ops`. Six read-only (`mode=ro` connection), **two
write-enabled**, clearly separated.

| # | Tool | Access | Reads / writes | Purpose |
| --- | --- | --- | --- | --- |
| 1 | `get_vendor_invoices` | read | `invoices`, `vendors`, `tickets`, `inventory`, `desk`, `cash_accounts` | Unpaid and overdue bills, with the stock they block |
| 2 | `get_lease_details` | read | `leases`, `tickets`, `desk`, `cash_accounts` | Rent of record vs. the notice that claimed it |
| 3 | `get_inventory_and_pricing` | read | `inventory`, `pricing`, `desk` | Stock by size, shortfall, cost floor |
| 4 | `get_open_tickets` | read | `tickets`, `desk` | The board the Boss routes from |
| 5 | `get_cash_balance` | read | `cash_accounts`, `payments`, `desk` | Balance plus what has already gone out |
| 6 | `draft_payment_voucher` | read | `invoices`, `leases`, `payments`, `cash_accounts`, `desk` | Prepares a disbursement. **Writes nothing** |
| 7 | `record_approved_payment` | **write** | `payments`, `cash_accounts`, `invoices` | The only tool that moves cash. Human-approved only |
| 8 | `update_ticket_status` | **write** | `tickets` | Moves a ticket along, appending to its notes |

---

## 10. Safety

### Financial guardrails

`record_approved_payment` is the single point where money leaves the account, and it
refuses the write — nothing is touched — on any of four conditions:

| Guardrail | Refusal | Verified |
| --- | --- | --- |
| **No agent self-approval** | `approved_by` matching a blocklist of agent/AI names is rejected | `'Boss' is an agent, not a human approver` |
| **No invented amounts** | The amount must equal the obligation to the cent | `approved $500.00 but the invoice is $840.00` |
| **No double payment** | A second payment against the same `(kind, ref_id)` is refused | `Already paid by payment 1` |
| **No overdraft** | The balance must cover the full amount; no partial payments | `$160.00 available, $5000.00 required` |

Agents cannot choose amounts at all: `draft_payment_voucher` reads the figure from the
invoice or lease row. The six read tools open the database with SQLite's `mode=ro` flag, so
most of the tool surface is physically incapable of writing.

**No ship with unpaid invoice.** Every prompt carries this rule. Goods tied to an unpaid,
overdue vendor invoice are not released and no delivery date is promised — the restock is
blocked until the bill clears, and the agents say so rather than hedging.

### Human approvals

Agents prepare; humans approve. `draft_payment_voucher` returns
`requires_human_approval: true` and status `awaiting_human_approval`, and that is the
finished state of an agent's work. `approve_voucher()` lives in the orchestrator, outside
the agent loop, and is called by the backend route a person clicks — with that person's
name. A ticket with an unapproved voucher ends `awaiting_approval`, never `resolved`, and
the run appends a `human_approval_required` event.

Tested: an agent calling `record_approved_payment` with `approved_by="Accounting"` mid-run
was rejected, and the balance did not move.

### Token and turn budgets

Enforced by `TokenBudget` in code — the prompts ask for restraint, these impose it:

| Limit | Default | Why |
| --- | --- | --- |
| `max_delegation_turns` | **10 per ticket** | Shared across all five agents; prevents hand-off ping-pong |
| `max_delegation_depth` | 3 | Stops A→B→A→B nesting |
| `max_requests_per_agent` | 12 | Model requests in one agent run |
| `max_tool_calls_per_agent` | 15 | MCP calls in one agent run |
| `max_total_tokens_per_agent` | 60,000 | Token ceiling per agent run |

The budget is shared, not per-agent: a specialist's hand-off spends the same pool as the
Boss's. When it runs out, further delegation is refused with an explanation telling the
agent to conclude with what it has, and a `budget_exhausted` event is appended.

### Audit trail

`output/audit_trail.json` is **append-only**. Each write reads the existing array, appends,
and swaps the file atomically with `os.replace`, so an interrupted write cannot truncate
it. A `.jsonl` mirror is a true `open(..., "a")` append. A corrupt file is moved aside
rather than overwritten. Events recorded: `run_started`, `agent_started`, `delegation`,
`delegation_refused`, `tool_call` (with arguments and verbatim result), `agent_finished`,
`voucher_prepared`, `human_approval_required`, `budget_exhausted`, `error`, `run_finished`.

Note the two clocks, deliberately kept apart: audit entries carry wall-clock timestamps
(when we ran), while every business date — overdue, due-in, `paid_at` — comes from
`desk.date_today`.

---

## 11. API Routes

`backend/main.py` (FastAPI). Run with `python -m uvicorn backend.main:app --port 8010`.

| Method | Endpoint | What it does |
| --- | --- | --- |
| `GET` | `/api/tickets` | All tickets with fields and status, plus `is_resolved` / `needs_human` flags, read through MCP. |
| `POST` | `/api/tickets/{id}/run` | Turns the five-agent team loose on one ticket; returns a `run_id` immediately, or blocks when `wait: true`. |
| `GET` | `/api/runs/{run_id}` | Status of a background run started above: running, finished, or failed with the error. |
| `GET` | `/api/events` | Recent agent activity — messages, delegations, tool calls, timestamps — filterable by `since_seq`, `ticket_id`, or `run_id` for live polling. |
| `POST` | `/api/payments/approve` | **A human approves a voucher.** The only route that writes to `payments` and debits `cash_accounts`. |
| `GET` | `/api/cash` | Current checking balance plus every payment already recorded against it. |
| `POST` | `/api/reset` | Restores `campus_customs_new.db` from the pristine seed for a fresh run; the audit trail is preserved. |
| `GET` | `/api/health` | Service health, MCP reachability, desk date, allowed origins, audit event count. |

**CORS** allows `http://localhost:5173` (the Vite frontend) and `127.0.0.1:5173`, plus the
`:5174` pair as a fallback because another project in this repo already holds 5173. Verified:
a preflight from `http://localhost:5173` returns that exact origin with credentials allowed.

**No route opens SQLite.** Tickets and cash are read with the same MCP tools the agents use,
so the dashboard and the agents cannot disagree about what is true. The one exception is
`/api/reset`, which copies a file rather than querying one.

### Verified against a running server

| Route | Result |
| --- | --- |
| `GET /api/tickets` | 3 tickets, all `open`, desk date 2026-08-31 |
| `GET /api/cash` | `3400.0`, no payments recorded |
| `GET /api/events` | returns events with `seq`, filterable by run |
| `POST /api/tickets/999/run` | `404 Ticket 999 is not on the board.` |
| `POST /api/payments/approve` with `approved_by: "Boss"` | `400` — agent names rejected, balance unchanged |
| `POST /api/payments/approve` with `amount: 500` | `400` — amount must match the $840.00 obligation |
| `POST /api/payments/approve` with `approved_by: "Chanseo Park"` | `200` — payment 1 recorded, `3400.00 → 2560.00`, invoice marked `paid` |
| Same approval repeated | `400 Already paid by payment 1. Refusing to double-pay.` |
| `POST /api/reset` | balance back to `3400.0`, ledger empty, all tickets `open`, working DB byte-identical to the seed |

A refused payment returns **400**, not 500 — the guardrail firing is the system working, not
an error.

### Running and re-testing

The app imports under either launch style:

```bash
python -m uvicorn backend.main:app --reload --port 8000          # from the project root
```

```bash
python -m uvicorn main:app --reload --port 8000 --app-dir backend   # backend/ on sys.path
```

`backend/main.py` puts the project root on `sys.path` at import time so the `backend.*`
imports resolve either way.

`backend/test_api.py` exercises all eight routes and asserts the two that change state —
the approval must debit `cash_accounts`, and the reset must restore the database file:

```bash
python backend/test_api.py --base http://127.0.0.1:8000
```

Last run: **34 checks, 34 passed, 0 failed**, including `3400.00 → 2560.00` on approval with a
`payments` row written under the approver's name, and a reset leaving the working database
byte-identical to the seed.

---

## 12. The Dashboard

`frontend/` — React 19 + Vite + TypeScript. Design rationale lives in
`output/design.md`.

```bash
python -m uvicorn backend.main:app --reload --port 8000     # API
cd frontend && npm run dev                                  # dashboard on :5173
```

Requests go to same-origin `/api/*` and Vite proxies them to the API, so the browser
never needs to know which port the backend is on. `VITE_API_TARGET` in `frontend/.env.local`
overrides the target (both 8000 and 8001 are held by other dev servers on this machine,
so local verification ran against 8011 with the dashboard on 5180).

| Panel | What it does |
| --- | --- |
| Masthead | Approver on duty, desk date, and the live checking balance |
| Ticket Board | All three tickets with requester, subject, facts, and a status badge |
| Actions | Select a ticket, **Run Agent Team**, or reset the desk to $3,400.00 |
| Approval cards | A prepared voucher with amount, payee, days overdue, projected balance, and the approve button |
| Ticket Summary | After a run: the Boss's conclusion, delegation chain, cited facts, blockers, next steps |
| Live Agent Activity | Every event as it lands — hand-offs, tool calls with arguments and results, conclusions |
| Payments Ledger | Appears once money has moved, showing who approved what |

### Verified in the browser

| Check | Result |
| --- | --- |
| Tickets load from the API | 101 Tauhid Zaman · 102 Elm City Properties · 103 Yale AI Club, all `Open` |
| Desk date in the masthead | `2026-08-31` |
| Opening balance | `$3,400.00` |
| Feed attribution | `Boss / hand-off`, `Inventory / tool call`, `Inventory → Accounting`, `Cust. Service / hand-off` — each agent in its own color |
| Approval card | `$840.00` to Bulldog Print Co, invoice 501, 3 days overdue, balance after `$2,560.00` |
| Approve (invoice 501) | Card flips to "Payment recorded", header moves `$3,400.00 → $2,560.00`, ledger row appears under the approver's name |
| Approve (rent, lease 1) | Header `$2,560.00 → $160.00` with a `− $2,400.00` delta and the gold flash |
| Settled vouchers | Drop off the pending list automatically |
| Reset button | Balance back to `$3,400.00`, badges back to `Open`, ledger panel gone, feed cleared |
| TypeScript | `tsc -b` clean |

**One real bug found and fixed during verification:** `/api/events` filtered on `seq`,
which restarts at 1 for every run, so a new run's early events were silently dropped by
the dashboard's polling cursor. Events now carry `index` — their position in the
append-only trail — and that is what the cursor uses.

### Build and approval timing

`npm run build` (`tsc -b && vite build`) completes clean: 21 modules, no type errors, no
warnings — `dist/index.html` 0.41 kB, CSS 11.82 kB (3.25 kB gzipped), JS 236.68 kB
(73.49 kB gzipped).

Approving a payment updates the header **in the same frame as the card**. The approval
response already carries the authoritative `balance_after`, so the UI applies it
immediately instead of waiting on `/api/cash` (which spawns an MCP subprocess and takes
3–5 s). Measured from the click:

| Moment | Elapsed |
| --- | --- |
| Card flips to "Payment recorded" | 2911 ms (when the POST returns) |
| Header moves `$3,400.00 → $2,560.00` | 2911 ms — same frame |
| `− $840.00` delta and gold flash | same frame |
| Ledger row + voucher removed from pending | on the follow-up refetch |

Two smaller fixes came out of this pass: the optimistic balance silently no-opped when
`/api/cash` had not answered yet (the header kept showing a dash), and a transient 502
from loading the dashboard before the API was up left a red banner on screen permanently.
Both now resolve themselves.

---

## 13. Resolution Run

All three tickets executed end to end on the desk date 2026-08-31, starting from a database
reset to the pristine seed.

### Execution mode — read this first

The Portkey key for `gpt-6-luna` is **over its usage limit** (HTTP 412, "Portkey API Key
Usage Limit Exceeded"), so the agent team could not reach a model. This run was carried out
by `backend/scripted_run.py`, which follows the plan written up front in
`output/desk_tickets.html`.

| Real | Scripted |
| --- | --- |
| Every MCP tool call and the values it returned | Which specialist to call, and when to hand off |
| Every database write: `payments`, `cash_accounts`, `invoices`, `tickets` | What each agent concluded |
| Every human approval, through `POST /api/payments/approve` with its guardrails | — |
| Every audit event, via the same append-only writer | — |

Audit events from this run carry `mode: "scripted"`. When a working key exists, running
`backend/orchestrator.py::run_ticket` rebuilds the same artifacts from genuine agent output.

### What happened

| Ticket | Chain | Hand-offs | MCP calls | Human approval | Cash |
| --- | --- | --- | --- | --- | --- |
| **101** | Boss → Inventory → Accounting → Customer Service | 3 / 10 | 6 | $840.00 by Chanseo Park | −$840.00 |
| **102** | Boss → Facilities → Accounting | 2 / 10 | 6 | $2,400.00 by Chanseo Park | −$2,400.00 |
| **103** | Boss → Customer Service → Inventory + Accounting | 3 / 10 | 7 | quote confirmed, no payment | $0.00 |

All three ended `resolved`. Five of the eight hand-offs were specialist-to-specialist; the
Boss made exactly one outbound call per ticket.

### Cash reconciliation

```
 3,400.00   starting balance
  -840.00   ticket 101 -> invoice 501, Bulldog Print Co      (payment id 1)
=2,560.00
-2,400.00   ticket 102 -> lease 1, Elm City Properties       (payment id 2)
=  160.00
     0.00   ticket 103 -> quote only, no payment row
=  160.00   ending balance
```

Read back from `data/campus_customs_new.db` after the run: `cash_accounts.balance = 160.0`,
exactly two `payments` rows totalling `3,240.00`, invoice 501 `status = 'paid'`, all three
tickets `status = 'resolved'`. Every `approved_by` names a human — **no agent appears in the
ledger**.

This is not asserted, it is checked. `backend/verify_cash.py` parses the numbers out of the
Cash tab's HTML and compares each one against the database:

```bash
python backend/verify_cash.py
```

**32 checks, 32 passed.** It covers the balance, both payment rows (amount, account, approver,
`paid_at`), the ledger total, the arithmetic `3,400.00 − 3,240.00 = 160.00`, every row of the
HTML cash table, both HTML ledger rows, invoice status, ticket statuses, and that no agent name
appears as an approver. A negative control confirms it bites: changing one figure in a copy of
the HTML from `160.00` to `170.00` produced `31 passed, 1 failed` and a non-zero exit.

### Artifacts

| File | Contents |
| --- | --- |
| `output/desk_tickets.html` | Expected (written before) and Actual (written after) per ticket, plus the reconciled Cash tab |
| `output/resolved_tickets.json` | Per ticket: final status, outcome, agent contributions, delegation chain, tools called, vouchers, human approvals |
| `output/resolved_board.html` | Final board snapshot — three resolved cards with chains, tools, approvals, and the cash table |
| `output/audit_trail.json` | 181 events total, 55 from this run, appended never overwritten |

---

## 14. Completeness

Where each required subject is documented.

| Subject | Section | State |
| --- | --- | --- |
| Database — tables, columns, workflow ties | §1–§5 | Complete |
| MCP server and the three ticket tools | §6 | Complete |
| MCP registration and smoke test | §7 | Complete |
| The five agents and the execution loop | §8 | Complete |
| All eight MCP tools | §9 | Complete |
| Safety — guardrails, approvals, budgets, audit | §10 | Complete |
| API routes | §11 | Complete |
| Dashboard | §12 | Complete |
| Resolution run and reconciliation | §13 | Complete |

### Repository map

```
data/campus_customs.db          pristine seed, never written
data/campus_customs_new.db      working copy the agents operate on
mcp_server/server.py            FastMCP server, 8 tools (6 read-only, 2 write)
mcp_server/README.md            tool-by-tool reference
backend/prompts/*.md            five agent prompts
backend/models.py               typed agent messages, delegations, tool results, ticket state
backend/agents.py               the five PydanticAI agents, gpt-6-luna pinned
backend/orchestrator.py         the execution loop and the human-approval path
backend/audit.py                append-only audit writer
backend/main.py                 FastAPI routes
backend/scripted_run.py         deterministic fallback runner (no LLM)
backend/test_api.py             34-check API suite
frontend/                       React + Vite + TypeScript dashboard
output/                         harness, design notes, desk tickets, resolved board, audit trail
.mcp.json                       MCP client registration
```

### Known limitations

1. **No live LLM run has happened.** The Portkey key is over quota, so every agent decision in
   the record is scripted rather than model-chosen. This is the single biggest gap in the
   project and is flagged in every artifact it touches.
2. **Each backend read spawns a fresh MCP subprocess**, so `/api/cash` and `/api/tickets` take
   3–5 seconds. A persistent MCP session would fix it; the dashboard works around it by
   applying the approval response's balance immediately.
3. **Ports.** 5173, 5174, 8000 and 8001 are all held by other dev servers on this machine, so
   local verification ran the API on 8011 and the dashboard on 5180.

---

