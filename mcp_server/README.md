# Campus Customs Operations MCP Server

An MCP (Model Context Protocol) server that gives the Campus Customs agent team
read access to the shop's books. Without it, an agent asked "is the tee in
stock?" or "how overdue is that invoice?" can only guess. With it, every answer
is a row out of SQLite.

- **Server file:** `mcp_server/server.py`
- **Server name:** `campus-customs-ops`
- **Framework:** [FastMCP](https://gofastmcp.com)
- **Database:** `data/campus_customs_new.db` — the working copy. The pristine
  seed `data/campus_customs.db` is never touched, so Problem 9 can reset the
  desk to its $3,400.00 starting state.

## Why this server exists

The three open tickets on the desk each stall on a fact that lives in a
different table:

| Ticket | Stalls on | Lives in |
| --- | --- | --- |
| 101 — Tauhid Zaman wants a size S Bulldog tee | the tee is at **qty 0**, and the reprint that fixes it is an **unpaid, overdue** invoice | `invoices`, `vendors`, `inventory` |
| 102 — Elm City Properties sends a rent notice | the real rent amount and due date, not what the email claims | `leases` |
| 103 — Yale AI Club wants 20 size M hoodies at a discount | the size-M count **and** the cost floor under the list price | `inventory`, `pricing` |

One tool per ticket, each returning exactly the facts needed to clear it.

## Design rules

1. **Read-only by construction.** The connection opens with SQLite's `mode=ro`
   URI flag, so the tools physically cannot write — an `UPDATE` raises
   `attempt to write a readonly database`. Payments and ticket status changes
   belong to later, explicitly write-enabled tools.
2. **No invented facts.** Every value returned comes from a `SELECT`. There are
   no default amounts, no placeholder dates, no hardcoded shop knowledge. When
   a row is missing the tool returns `found=False` with the reason and the
   list of values that *do* exist, so the agent can correct itself rather than
   hallucinate.
3. **The desk owns the clock.** "Overdue" and "due in N days" are computed
   against `desk.date_today` (`2026-08-31`), never `datetime.now()`. Every
   response carries `as_of` so the agent can see which clock it was judged by.
4. **Money is rounded at the boundary.** SQLite stores amounts as `REAL`;
   every amount is rounded to 2 decimals on the way out so the cash
   reconciliation lands exactly on $160.00.
5. **Every response names its sources.** `source_tables` lists the tables read,
   so the audit trail can show where a claim came from.
6. **The schema is the documentation.** Tools are named `get_<what it returns>`;
   every argument is `Annotated[...]` with a description and a `Literal` where the
   values are fixed; every tool returns a declared Pydantic model, not a bare
   `dict`. An agent reading the tool list sees field names, types, and meanings —
   including warnings like "`total_units_all_sizes`: do NOT read this as
   availability of one size" — before it ever makes a call.

## The tools

Eight tools: six read-only, two write-enabled. The read tools open the database with
SQLite's `mode=ro` flag; only `record_approved_payment` and `update_ticket_status` use a
read-write connection.

| # | Tool | Access | Unlocks |
| --- | --- | --- | --- |
| 1 | `get_vendor_invoices` | read | Ticket 101 |
| 2 | `get_lease_details` | read | Ticket 102 |
| 3 | `get_inventory_and_pricing` | read | Ticket 103 |
| 4 | `get_open_tickets` | read | The board the Boss routes from |
| 5 | `get_cash_balance` | read | Affordability before any recommendation |
| 6 | `draft_payment_voucher` | read | Prepares a payment. Writes nothing |
| 7 | `record_approved_payment` | **write** | The only tool that moves cash |
| 8 | `update_ticket_status` | **write** | Moves a ticket along the board |

### Ticket tools

### 1. `get_vendor_invoices(status="open", sku=None) -> VendorInvoiceReport` → ticket 101

Inspects vendor invoices — including reprint orders — and flags which are
overdue. Reads `invoices` joined to `vendors`, plus the `tickets` pointing at
each invoice and the live `inventory` count for what those tickets ordered.

Per invoice it returns: amount, due date, status, description, `days_past_due`
and `is_overdue` against the reference date, the vendor with its `lead_days`,
`earliest_delivery_if_paid_today` (reference date + lead days), and the linked
tickets with `qty_on_hand`. The current checking balance rides along so
Accounting can see affordability in the same call.

Against the seeded desk it surfaces invoice **501 — $840.00, due 2026-08-28,
3 days past due**, vendor Bulldog Print Co (5 lead days), linked to ticket 101
whose `CC-TEE-WHITE / S` is at **0 on hand**, with delivery no earlier than
**2026-09-05** if paid today. That is the whole of ticket 101 in one call.

### 2. `get_lease_details(lease_id=None, due_within_days=None) -> LeaseDetailReport` → ticket 102

Fetches lease obligations from `leases`: landlord, space, monthly rent, next
due date, plus `days_until_due` and `is_overdue` against the reference date,
the tickets attached to the lease, and whether checking covers the rent.

Against the seeded desk: lease **1, Chapel Street shop, Elm City Properties,
$2,400.00, due 2026-09-02 — 2 days out**, covered by checking. This is what
turns ticket 102's email from a claim into a verified obligation; an unknown
`lease_id` returns `found=False` and the IDs that do exist.

### 3. `get_inventory_and_pricing(sku, size=None, qty=None) -> InventoryPricingReport` → ticket 103

Checks on-hand stock by size from `inventory` and the cost/price floor from
`pricing`. Stock comes back per size — `inventory` is keyed on `(sku, size)`,
so a healthy total across sizes says nothing about the size actually ordered.
Pricing is keyed on SKU alone, so one `unit_cost` / `list_price` governs the
whole quote.

With `size` and `qty` supplied it adds the shortfall and the quote math.
Against the seeded desk, `("CC-HOOD-NAVY", "M", 20)` returns 8 on hand against
20 requested — a **shortfall of 12** — alongside $58.00 list over $22.00 cost:
**$1,160.00 revenue at list, $440.00 cost, $720.00 gross margin**, with
**62.1%** as the deepest discount before the sale prices at cost. Unknown SKUs
and unstocked sizes return `found=False` / `exists=False` with the valid
options listed.

### 4. `get_open_tickets(status="open") -> TicketBoardReport`

The ticket board: every ticket with its type, requester, subject, and the foreign keys that
say which specialist owns it. The Boss starts here. Ticket `notes` are the requester's
claim, not a verified fact.

### 5. `get_cash_balance(account="checking") -> CashBalanceReport`

The authoritative balance, plus `payments_recorded` — every disbursement already made from
that account. Call it before recommending a payment, both to confirm affordability and to
avoid paying something twice. Against the seeded desk: `3400.0`, no payments yet.

### 6. `draft_payment_voucher(kind, ref_id, account="checking") -> PaymentVoucher`

Prepares a disbursement **for a human to approve**. Writes nothing and moves no cash.

The amount is read from the invoice or lease — an agent cannot propose a number of its own.
The voucher returns `requires_human_approval: true` and status `awaiting_human_approval`,
with the balance before and the projected balance after. It refuses to draft when the
obligation does not exist, is already paid, or would overdraw the account.

`draft_payment_voucher("invoice", 501)` against the seeded desk returns `VCH-INVOICE-501`,
Bulldog Print Co, $840.00, 3 days past due, $3,400.00 → $2,560.00.

### 7. `record_approved_payment(kind, ref_id, amount, approved_by, account) -> PaymentResult`

**The only tool that moves cash.** It writes the `payments` row, debits the account, and
marks the obligation settled, in one transaction.

An agent must never call this on its own authority. Four guardrails refuse the write
outright, and nothing is touched when they fire:

| Guardrail | Example refusal |
| --- | --- |
| `approved_by` must name a human | `'Boss' is an agent, not a human approver.` |
| Amount must match the obligation | `approved $500.00 but the invoice is $840.00` |
| No double payment | `Already paid by payment 1. Refusing to double-pay.` |
| No overdraft | `$160.00 available, $5000.00 required.` |

### 8. `update_ticket_status(ticket_id, status, note) -> TicketUpdateResult`

Moves a ticket to `open`, `in_progress`, `awaiting_approval`, `resolved`, or `blocked`.
Notes are **appended**, never replaced, each stamped with the desk date, so a ticket keeps
its history. `awaiting_approval` is the correct state while a voucher is still unapproved.


## Running it

The server speaks stdio by default (how MCP clients launch it) and can serve
HTTP for manual poking:

```bash
python mcp_server/server.py
```

```bash
python mcp_server/server.py --http --port 8003
```

Set `CAMPUS_CUSTOMS_DB` to point at a different database file; it defaults to
`data/campus_customs_new.db` resolved relative to the project root.

### Environment note

`fastmcp` must be importable. The interpreter at `mcp_servers/.venv/` in this
repo has a working FastMCP 4.0.10 and is what the server was tested against;
the machine-wide Python has a FastMCP install that fails to import server
support. Client wiring in `.mcp.json` is Problem 4.
