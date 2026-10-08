"""Campus Customs operations MCP server.

Exposes the shop's SQLite desk to the agent team as three read-only tools, one
per open ticket:

    get_vendor_invoices       -> ticket 101 (unpaid / overdue vendor bills)
    get_lease_details         -> ticket 102 (rent amounts and due dates)
    get_inventory_and_pricing -> ticket 103 (stock by size + cost floor)

Naming and typing are deliberate: every tool is `get_<what it returns>`, every
argument is annotated with a description, and every tool returns a declared
Pydantic model rather than a bare dict, so an LLM reading the tool schema can
see the exact field names, types, and meanings before it ever makes a call.

Every number these tools return is read out of data/campus_customs_new.db. The
server has no fallback values, no defaults for missing rows, and no knowledge
of the shop baked into the code: when a row is absent the tool says so with
`found=False` instead of guessing. Dates are judged against desk.date_today,
never against the wall clock, so "overdue" means the same thing to every agent.

The database is opened read-only (SQLite `mode=ro`), so these tools physically
cannot mutate the desk. Payments and ticket updates belong to later, explicitly
write-enabled tools.

Run (stdio):  python mcp_server/server.py
Run (HTTP):   python mcp_server/server.py --http   ->  http://127.0.0.1:8003/mcp
"""

from __future__ import annotations

import argparse
import os
import sqlite3
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from pydantic import BaseModel, Field

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent

# The working copy the agents operate on. campus_customs.db stays pristine.
DB_PATH = Path(
    os.getenv("CAMPUS_CUSTOMS_DB", PROJECT_ROOT / "data" / "campus_customs_new.db")
).resolve()

mcp = FastMCP("campus-customs-ops")


# --------------------------------------------------------------------------
# returned models - these are the tool documentation an LLM actually reads
# --------------------------------------------------------------------------


class CheckingAccount(BaseModel):
    """The shop's cash position, straight from cash_accounts."""

    account: str = Field(description="Account name, e.g. 'checking'.")
    balance: float = Field(description="Current balance in USD, rounded to cents.")
    as_of: str = Field(description="Date the balance was stated, ISO yyyy-mm-dd.")


class Vendor(BaseModel):
    """A supplier row from vendors."""

    id: int = Field(description="Vendor id, referenced by invoices.vendor_id.")
    name: str | None = Field(description="Vendor name, e.g. 'Bulldog Print Co'.")
    specialty: str | None = Field(description="What this vendor supplies, e.g. 'apparel reprint'.")
    lead_days: int | None = Field(description="Days from order to delivery for this vendor.")


class OrderTicketRef(BaseModel):
    """A ticket attached to an invoice, with live stock for what it ordered."""

    ticket_id: int = Field(description="Ticket id, e.g. 101.")
    type: str = Field(description="Ticket type, e.g. 'customer_order'.")
    requester: str = Field(description="Who opened the ticket.")
    subject: str = Field(description="One-line ticket subject.")
    sku: str | None = Field(description="SKU the ticket is about, if any.")
    size: str | None = Field(description="Size the ticket is about, if any.")
    qty_requested: int | None = Field(description="Units the requester asked for.")
    status: str = Field(description="Ticket status, e.g. 'open'.")
    qty_on_hand: int | None = Field(
        default=None,
        description="Units of that exact (sku, size) currently on the shelf. 0 means a stockout.",
    )


class VendorInvoice(BaseModel):
    """One invoice, with everything needed to decide whether to pay it now."""

    invoice_id: int = Field(description="Invoice id, e.g. 501.")
    vendor: Vendor = Field(description="The vendor this invoice is owed to.")
    amount: float = Field(description="Amount owed in USD, rounded to cents.")
    due_date: str = Field(description="Date payment is due, ISO yyyy-mm-dd.")
    status: str = Field(description="Invoice status, e.g. 'open' or 'paid'.")
    description: str | None = Field(
        description="Free-text line, e.g. 'Rush reprint CC-TEE-WHITE S'."
    )
    days_past_due: int = Field(
        description="Days between due_date and the desk reference date. Positive means late."
    )
    is_overdue: bool = Field(description="True when the invoice is unpaid and past its due date.")
    earliest_delivery_if_paid_today: str | None = Field(
        default=None,
        description="Reference date plus the vendor's lead_days. The soonest goods can arrive.",
    )
    linked_tickets: list[OrderTicketRef] = Field(
        default_factory=list, description="Tickets whose invoice_id points at this invoice."
    )


class VendorInvoiceReport(BaseModel):
    """Return type of get_vendor_invoices."""

    as_of: str = Field(description="desk.date_today - the reference date every judgement used.")
    checking: CheckingAccount | None = Field(
        description="Cash available to settle these invoices."
    )
    invoice_count: int = Field(description="Number of invoices matching the filter.")
    total_amount: float = Field(description="Sum of the matching invoice amounts in USD.")
    invoices: list[VendorInvoice] = Field(
        default_factory=list, description="Matching invoices, soonest due date first."
    )
    source_tables: list[str] = Field(description="Tables read to build this answer.")


class LeaseTicketRef(BaseModel):
    """A ticket attached to a lease."""

    ticket_id: int = Field(description="Ticket id, e.g. 102.")
    type: str = Field(description="Ticket type, e.g. 'rent_notice'.")
    requester: str = Field(description="Who opened the ticket, e.g. the landlord.")
    subject: str = Field(description="One-line ticket subject.")
    status: str = Field(description="Ticket status, e.g. 'open'.")
    notes: str | None = Field(description="Ticket notes - treat as a claim, not as fact.")


class LeaseObligation(BaseModel):
    """One lease, as recorded - the authoritative version of any rent claim."""

    lease_id: int = Field(description="Lease id, referenced by tickets.lease_id.")
    space_name: str = Field(description="The leased space, e.g. 'Chapel Street shop'.")
    landlord: str = Field(description="Who gets paid, e.g. 'Elm City Properties'.")
    monthly_rent: float = Field(description="Rent owed per month in USD, rounded to cents.")
    next_due: str = Field(description="Next rent due date, ISO yyyy-mm-dd.")
    days_until_due: int = Field(
        description="Days from the desk reference date to next_due. Negative means already late."
    )
    is_overdue: bool = Field(description="True when next_due is before the reference date.")
    covered_by_checking: bool | None = Field(
        description="True when the current checking balance is at least monthly_rent."
    )
    notes: str | None = Field(description="Lease notes, if any.")
    linked_tickets: list[LeaseTicketRef] = Field(
        default_factory=list, description="Tickets whose lease_id points at this lease."
    )


class LeaseDetailReport(BaseModel):
    """Return type of get_lease_details."""

    as_of: str = Field(description="desk.date_today - the reference date every judgement used.")
    found: bool = Field(
        description="False only when a specific lease_id was requested and does not exist."
    )
    not_found_reason: str | None = Field(
        default=None, description="Why found is False. Null on success."
    )
    available_lease_ids: list[int] = Field(
        default_factory=list, description="Lease ids that do exist. Populated when found is False."
    )
    checking: CheckingAccount | None = Field(description="Cash available to pay this rent.")
    lease_count: int = Field(description="Number of leases matching the filter.")
    total_monthly_rent: float = Field(description="Sum of monthly rent across matching leases.")
    leases: list[LeaseObligation] = Field(
        default_factory=list, description="Matching leases, soonest due date first."
    )
    source_tables: list[str] = Field(description="Tables read to build this answer.")


class SizeStock(BaseModel):
    """On-hand units for one (sku, size) pair - the real unit of inventory."""

    size: str = Field(description="Size label, e.g. 'M' or 'OS'.")
    qty: int = Field(description="Units on hand for this size. 0 means a stockout.")
    location: str = Field(description="Where it sits in the shop, e.g. 'Aisle A'.")
    in_stock: bool = Field(description="True when qty is greater than 0.")


class Pricing(BaseModel):
    """The pricing row for a SKU. Keyed on SKU only - never on size."""

    unit_cost: float = Field(description="What the shop pays per unit. The discount floor.")
    list_price: float = Field(description="Standard selling price per unit in USD.")
    unit_gross_margin: float = Field(description="list_price minus unit_cost, per unit.")
    gross_margin_pct: float | None = Field(description="Gross margin as a percent of list price.")
    max_discount_pct_before_selling_at_cost: float | None = Field(
        description="Deepest discount off list that still prices at or above unit_cost."
    )


class RequestedSizeStock(BaseModel):
    """Stock for the one size a ticket actually asked for."""

    size: str = Field(description="The size requested.")
    exists: bool = Field(description="False when this SKU is not stocked in that size at all.")
    available_sizes: list[str] = Field(
        default_factory=list, description="Sizes that do exist. Populated when exists is False."
    )
    qty_on_hand: int | None = Field(default=None, description="Units on hand in the requested size.")
    location: str | None = Field(default=None, description="Where that size is shelved.")
    qty_requested: int | None = Field(default=None, description="Units the ticket asked for.")
    shortfall: int | None = Field(
        default=None,
        description="Units that must be produced or bought: qty_requested minus qty_on_hand, floored at 0.",
    )
    can_fulfill_from_stock: bool | None = Field(
        default=None, description="True only when the requested size alone covers the order."
    )


class QuoteMath(BaseModel):
    """Extended economics of a quantity at list price. Moves no cash."""

    qty: int = Field(description="Units quoted.")
    revenue_at_list: float = Field(description="list_price times qty, before any discount.")
    total_cost: float = Field(description="unit_cost times qty.")
    gross_margin_at_list: float = Field(description="revenue_at_list minus total_cost.")
    price_floor_per_unit: float = Field(
        description="unit_cost. Any discounted price below this sells at a loss."
    )
    note: str = Field(description="Reminder that a quote does not debit cash_accounts.")


class InventoryPricingReport(BaseModel):
    """Return type of get_inventory_and_pricing."""

    as_of: str = Field(description="desk.date_today - the reference date this was read on.")
    sku: str = Field(description="The SKU queried.")
    found: bool = Field(description="False when the SKU is in neither inventory nor pricing.")
    not_found_reason: str | None = Field(default=None, description="Why found is False.")
    known_skus: list[str] = Field(
        default_factory=list, description="SKUs that do exist. Populated when found is False."
    )
    product_name: str | None = Field(default=None, description="Display name from inventory.")
    stock_by_size: list[SizeStock] = Field(
        default_factory=list,
        description="On-hand units per size. Inventory is keyed on (sku, size).",
    )
    total_units_all_sizes: int = Field(
        default=0,
        description="Units across every size. Do NOT read this as availability of one size.",
    )
    pricing: Pricing | None = Field(default=None, description="Cost and list price for the SKU.")
    pricing_warning: str | None = Field(
        default=None, description="Set when the SKU is stocked but has no pricing row."
    )
    requested_size: RequestedSizeStock | None = Field(
        default=None, description="Populated when a size argument was supplied."
    )
    quote: QuoteMath | None = Field(
        default=None, description="Populated when a qty argument was supplied and pricing exists."
    )
    source_tables: list[str] = Field(description="Tables read to build this answer.")


class TicketRecord(BaseModel):
    """One ticket exactly as the desk recorded it."""

    ticket_id: int = Field(description="Ticket id, e.g. 101.")
    type: str = Field(
        description="Routing key: 'customer_order', 'rent_notice', or 'price_override'."
    )
    requester: str = Field(description="Who opened the ticket.")
    subject: str = Field(description="One-line subject.")
    sku: str | None = Field(description="SKU involved, if any.")
    size: str | None = Field(description="Size involved, if any.")
    qty: int | None = Field(description="Units requested, if any.")
    lease_id: int | None = Field(description="Set when the ticket concerns a lease.")
    invoice_id: int | None = Field(description="Set when the ticket concerns an invoice.")
    status: str = Field(description="Ticket status, e.g. 'open' or 'resolved'.")
    notes: str | None = Field(description="Ticket notes. A claim from the requester, not a fact.")
    created_at: str = Field(description="When the ticket was opened, ISO timestamp.")


class TicketBoardReport(BaseModel):
    """Return type of get_open_tickets."""

    as_of: str = Field(description="desk.date_today - the shop's reference date.")
    ticket_count: int = Field(description="Number of tickets matching the filter.")
    tickets: list[TicketRecord] = Field(default_factory=list, description="Matching tickets by id.")
    source_tables: list[str] = Field(description="Tables read to build this answer.")


class PaymentRecord(BaseModel):
    """A disbursement already written to the ledger."""

    payment_id: int = Field(description="payments.id.")
    kind: str = Field(description="What was paid: 'invoice' or 'rent'.")
    ref_id: int | None = Field(description="invoices.id or leases.id, per kind.")
    amount: float = Field(description="Amount disbursed in USD.")
    account: str = Field(description="Account debited.")
    paid_at: str = Field(description="Date of the disbursement.")
    approved_by: str = Field(description="The human who approved it. Never an agent.")


class CashBalanceReport(BaseModel):
    """Return type of get_cash_balance."""

    as_of: str = Field(description="desk.date_today - the shop's reference date.")
    found: bool = Field(description="False when the requested account does not exist.")
    not_found_reason: str | None = Field(default=None, description="Why found is False.")
    available_accounts: list[str] = Field(
        default_factory=list, description="Accounts that do exist. Populated when found is False."
    )
    account: str | None = Field(default=None, description="Account name.")
    balance: float | None = Field(
        default=None, description="Authoritative current balance in USD. The ceiling on spending."
    )
    balance_as_of: str | None = Field(default=None, description="Date the balance was stated.")
    payments_recorded: list[PaymentRecord] = Field(
        default_factory=list,
        description="Disbursements already made from this account. Check before paying again.",
    )
    total_disbursed: float = Field(
        default=0.0, description="Sum of payments already recorded against this account."
    )
    source_tables: list[str] = Field(description="Tables read to build this answer.")


class PaymentVoucher(BaseModel):
    """A prepared, unapproved disbursement. Writes nothing and moves no cash."""

    as_of: str = Field(description="desk.date_today - the shop's reference date.")
    drafted: bool = Field(description="False when the obligation could not be drafted.")
    blocked_reason: str | None = Field(
        default=None, description="Why drafted is False, e.g. the obligation is already paid."
    )
    voucher_id: str | None = Field(
        default=None, description="Stable handle for this draft, e.g. 'VCH-INVOICE-501'."
    )
    kind: str = Field(description="'invoice' or 'rent'.")
    ref_id: int = Field(description="invoices.id or leases.id, per kind.")
    payee: str | None = Field(default=None, description="Who would be paid, read from the record.")
    amount: float | None = Field(
        default=None, description="Amount owed, read from the obligation. Never agent-chosen."
    )
    due_date: str | None = Field(default=None, description="When the obligation is due.")
    days_past_due: int | None = Field(
        default=None, description="Positive when already late, measured against the reference date."
    )
    memo: str | None = Field(default=None, description="What this payment is for.")
    account: str = Field(description="Account that would be debited.")
    balance_before: float | None = Field(default=None, description="Account balance before payment.")
    balance_after_if_approved: float | None = Field(
        default=None, description="Projected balance if a human approves this voucher."
    )
    sufficient_funds: bool | None = Field(
        default=None, description="False when the balance cannot cover the amount."
    )
    requires_human_approval: bool = Field(
        default=True,
        description="Always True. No agent may approve its own voucher; a human must.",
    )
    status: str = Field(
        default="awaiting_human_approval",
        description="Lifecycle state of the draft. Drafting never disburses.",
    )
    source_tables: list[str] = Field(description="Tables read to build this answer.")


class PaymentResult(BaseModel):
    """Return type of record_approved_payment. The only tool that moves cash."""

    as_of: str = Field(description="desk.date_today - the date stamped on the payment.")
    recorded: bool = Field(description="True only when the ledger and balance were both written.")
    rejected_reason: str | None = Field(
        default=None,
        description="Why the payment was refused: no approver, wrong amount, duplicate, or overdraft.",
    )
    payment_id: int | None = Field(default=None, description="New payments.id when recorded.")
    kind: str = Field(description="'invoice' or 'rent'.")
    ref_id: int = Field(description="invoices.id or leases.id, per kind.")
    amount: float | None = Field(default=None, description="Amount actually disbursed.")
    approved_by: str | None = Field(default=None, description="The human who approved it.")
    account: str = Field(description="Account debited.")
    balance_before: float | None = Field(default=None, description="Balance before the debit.")
    balance_after: float | None = Field(default=None, description="Balance after the debit.")
    obligation_status: str | None = Field(
        default=None, description="Status of the invoice or lease after the payment."
    )
    source_tables: list[str] = Field(description="Tables read or written.")


class TicketUpdateResult(BaseModel):
    """Return type of update_ticket_status."""

    as_of: str = Field(description="desk.date_today - the shop's reference date.")
    updated: bool = Field(description="True when the ticket row was written.")
    rejected_reason: str | None = Field(default=None, description="Why the update was refused.")
    ticket_id: int = Field(description="Ticket that was targeted.")
    previous_status: str | None = Field(default=None, description="Status before the update.")
    new_status: str | None = Field(default=None, description="Status after the update.")
    notes: str | None = Field(default=None, description="Notes field after the update.")
    allowed_statuses: list[str] = Field(
        default_factory=list, description="Statuses this tool accepts."
    )
    source_tables: list[str] = Field(description="Tables read or written.")


# --------------------------------------------------------------------------
# database access
# --------------------------------------------------------------------------


def _connect() -> sqlite3.Connection:
    """Open the working database read-only, with foreign keys enforced."""
    if not DB_PATH.exists():
        raise FileNotFoundError(
            f"Campus Customs database not found at {DB_PATH}. "
            "Copy data/campus_customs.db to data/campus_customs_new.db first."
        )
    uri = f"file:{DB_PATH.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _connect_rw() -> sqlite3.Connection:
    """Open the working database for WRITING. Used only by the two tools that
    change the desk: record_approved_payment and update_ticket_status."""
    if not DB_PATH.exists():
        raise FileNotFoundError(f"Campus Customs database not found at {DB_PATH}.")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _desk_date(conn: sqlite3.Connection) -> str:
    """The shop's reference date. Single source of 'now' for every agent."""
    row = conn.execute("SELECT date_today FROM desk LIMIT 1").fetchone()
    if row is None:
        raise ValueError("desk table is empty: no reference date available")
    return str(row["date_today"])


def _checking(conn: sqlite3.Connection) -> CheckingAccount | None:
    row = conn.execute(
        "SELECT name, balance, date FROM cash_accounts WHERE name = 'checking' LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    return CheckingAccount(account=row["name"], balance=_money(row["balance"]), as_of=row["date"])


def _money(value: Any) -> float:
    """Amounts are REAL in SQLite; round at every boundary so cash reconciles."""
    return round(float(value), 2)


def _days_between(from_iso: str, to_iso: str) -> int:
    return (date.fromisoformat(to_iso) - date.fromisoformat(from_iso)).days


# --------------------------------------------------------------------------
# tool 1 - ticket 101
# --------------------------------------------------------------------------


@mcp.tool
def get_vendor_invoices(
    status: Annotated[
        Literal["open", "paid", "all"],
        Field(description="Which invoices to return. 'all' ignores status entirely."),
    ] = "open",
    sku: Annotated[
        str | None,
        Field(
            description="Optional SKU filter, e.g. 'CC-TEE-WHITE'. Matches invoices whose linked "
            "ticket is for that SKU, or whose description mentions it."
        ),
    ] = None,
) -> VendorInvoiceReport:
    """Get vendor invoices, including reprint orders, with overdue status and stock impact.

    Use this before promising a customer anything about an out-of-stock item: a
    reprint that has been ordered but not paid will not ship. Each invoice comes
    back with its vendor, the vendor's lead time, how many days past due it is
    relative to the shop's reference date, the tickets that point at it, and the
    current on-hand count for the SKU and size those tickets want.

    Returns a VendorInvoiceReport. An empty invoice list means nothing matched -
    it is not an error, and never a reason to assume an invoice exists.
    """
    with closing(_connect()) as conn:
        today = _desk_date(conn)

        sql = (
            "SELECT i.id, i.vendor_id, i.amount, i.due_date, i.status, i.description, "
            "       v.name AS vendor_name, v.specialty AS vendor_specialty, "
            "       v.lead_days AS vendor_lead_days "
            "FROM invoices i LEFT JOIN vendors v ON v.id = i.vendor_id"
        )
        params: list[Any] = []
        if status != "all":
            sql += " WHERE i.status = ?"
            params.append(status)
        sql += " ORDER BY i.due_date ASC"

        invoices: list[VendorInvoice] = []
        for row in conn.execute(sql, params).fetchall():
            linked = _linked_tickets_for_invoice(conn, row["id"])

            if sku is not None:
                in_tickets = any(t.sku == sku for t in linked)
                in_desc = sku.lower() in (row["description"] or "").lower()
                if not (in_tickets or in_desc):
                    continue

            days_past_due = _days_between(row["due_date"], today)
            earliest_delivery = None
            if row["vendor_lead_days"] is not None:
                earliest_delivery = (
                    date.fromisoformat(today) + timedelta(days=int(row["vendor_lead_days"]))
                ).isoformat()

            invoices.append(
                VendorInvoice(
                    invoice_id=row["id"],
                    vendor=Vendor(
                        id=row["vendor_id"],
                        name=row["vendor_name"],
                        specialty=row["vendor_specialty"],
                        lead_days=row["vendor_lead_days"],
                    ),
                    amount=_money(row["amount"]),
                    due_date=row["due_date"],
                    status=row["status"],
                    description=row["description"],
                    days_past_due=days_past_due,
                    is_overdue=days_past_due > 0 and row["status"] != "paid",
                    earliest_delivery_if_paid_today=earliest_delivery,
                    linked_tickets=linked,
                )
            )

        return VendorInvoiceReport(
            as_of=today,
            checking=_checking(conn),
            invoice_count=len(invoices),
            total_amount=_money(sum(i.amount for i in invoices)),
            invoices=invoices,
            source_tables=["invoices", "vendors", "tickets", "inventory", "desk", "cash_accounts"],
        )


def _linked_tickets_for_invoice(conn: sqlite3.Connection, invoice_id: int) -> list[OrderTicketRef]:
    """Tickets pointing at this invoice, with live stock for what they ordered."""
    rows = conn.execute(
        "SELECT id, type, requester, subject, sku, size, qty, status "
        "FROM tickets WHERE invoice_id = ? ORDER BY id",
        (invoice_id,),
    ).fetchall()

    linked: list[OrderTicketRef] = []
    for row in rows:
        qty_on_hand: int | None = None
        if row["sku"] is not None and row["size"] is not None:
            stock = conn.execute(
                "SELECT qty FROM inventory WHERE sku = ? AND size = ?",
                (row["sku"], row["size"]),
            ).fetchone()
            qty_on_hand = stock["qty"] if stock is not None else None

        linked.append(
            OrderTicketRef(
                ticket_id=row["id"],
                type=row["type"],
                requester=row["requester"],
                subject=row["subject"],
                sku=row["sku"],
                size=row["size"],
                qty_requested=row["qty"],
                status=row["status"],
                qty_on_hand=qty_on_hand,
            )
        )
    return linked


# --------------------------------------------------------------------------
# tool 2 - ticket 102
# --------------------------------------------------------------------------


@mcp.tool
def get_lease_details(
    lease_id: Annotated[
        int | None,
        Field(description="Specific lease to fetch. Omit for every lease on file."),
    ] = None,
    due_within_days: Annotated[
        int | None,
        Field(
            description="Keep only leases due within this many days of the reference date. "
            "Already-overdue leases always qualify."
        ),
    ] = None,
) -> LeaseDetailReport:
    """Get lease details: landlord, monthly rent, next due date, and days remaining.

    Use this to verify a rent notice before paying it. The email quoted in a
    ticket is a claim; the lease row is the fact, and this tool returns the
    fact, plus how many days remain before the rent is late and whether the
    checking balance currently covers it.

    Returns a LeaseDetailReport. A requested lease_id that does not exist comes
    back with found=False and the lease ids that do exist - never a fabricated
    rent amount.
    """
    with closing(_connect()) as conn:
        today = _desk_date(conn)
        checking = _checking(conn)

        if lease_id is not None:
            rows = conn.execute("SELECT * FROM leases WHERE id = ?", (lease_id,)).fetchall()
            if not rows:
                return LeaseDetailReport(
                    as_of=today,
                    found=False,
                    not_found_reason=f"No lease with id {lease_id} exists in the leases table.",
                    available_lease_ids=[
                        r["id"] for r in conn.execute("SELECT id FROM leases ORDER BY id")
                    ],
                    checking=checking,
                    lease_count=0,
                    total_monthly_rent=0.0,
                    leases=[],
                    source_tables=["leases", "desk", "cash_accounts"],
                )
        else:
            rows = conn.execute("SELECT * FROM leases ORDER BY next_due ASC").fetchall()

        leases: list[LeaseObligation] = []
        for row in rows:
            days_until_due = _days_between(today, row["next_due"])
            if due_within_days is not None and days_until_due > due_within_days:
                continue

            rent = _money(row["monthly_rent"])
            leases.append(
                LeaseObligation(
                    lease_id=row["id"],
                    space_name=row["space_name"],
                    landlord=row["landlord"],
                    monthly_rent=rent,
                    next_due=row["next_due"],
                    days_until_due=days_until_due,
                    is_overdue=days_until_due < 0,
                    covered_by_checking=None if checking is None else checking.balance >= rent,
                    notes=row["notes"],
                    linked_tickets=_linked_tickets_for_lease(conn, row["id"]),
                )
            )

        return LeaseDetailReport(
            as_of=today,
            found=True,
            checking=checking,
            lease_count=len(leases),
            total_monthly_rent=_money(sum(lease.monthly_rent for lease in leases)),
            leases=leases,
            source_tables=["leases", "tickets", "desk", "cash_accounts"],
        )


def _linked_tickets_for_lease(conn: sqlite3.Connection, lease_id: int) -> list[LeaseTicketRef]:
    rows = conn.execute(
        "SELECT id, type, requester, subject, status, notes FROM tickets "
        "WHERE lease_id = ? ORDER BY id",
        (lease_id,),
    ).fetchall()
    return [
        LeaseTicketRef(
            ticket_id=r["id"],
            type=r["type"],
            requester=r["requester"],
            subject=r["subject"],
            status=r["status"],
            notes=r["notes"],
        )
        for r in rows
    ]


# --------------------------------------------------------------------------
# tool 3 - ticket 103
# --------------------------------------------------------------------------


@mcp.tool
def get_inventory_and_pricing(
    sku: Annotated[str, Field(description="SKU to look up, e.g. 'CC-HOOD-NAVY'.")],
    size: Annotated[
        str | None,
        Field(description="Size the customer asked for, e.g. 'M'. Drives the shortfall math."),
    ] = None,
    qty: Annotated[
        int | None,
        Field(description="Units requested. Drives the shortfall and the extended quote math."),
    ] = None,
) -> InventoryPricingReport:
    """Get on-hand stock by size and the cost/list pricing for one SKU.

    Use this before quoting a bulk order or granting a discount. Stock is keyed
    on (sku, size), so this returns every size individually - a healthy total
    across sizes does not mean the requested size is available. Pricing is keyed
    on SKU alone, so one unit_cost and list_price govern the whole quote, and
    unit_cost is the floor any discount has to clear.

    Returns an InventoryPricingReport. Unknown SKUs come back with found=False
    and the SKUs that do exist; a size that is not stocked comes back with
    requested_size.exists=False and the sizes that are.
    """
    with closing(_connect()) as conn:
        today = _desk_date(conn)

        stock_rows = conn.execute(
            "SELECT sku, name, size, qty, location FROM inventory WHERE sku = ? ORDER BY size",
            (sku,),
        ).fetchall()
        price_row = conn.execute(
            "SELECT sku, unit_cost, list_price FROM pricing WHERE sku = ?", (sku,)
        ).fetchone()

        if not stock_rows and price_row is None:
            return InventoryPricingReport(
                as_of=today,
                sku=sku,
                found=False,
                not_found_reason=(
                    f"SKU {sku!r} appears in neither the inventory nor the pricing table."
                ),
                known_skus=[
                    r["sku"]
                    for r in conn.execute("SELECT DISTINCT sku FROM inventory ORDER BY sku")
                ],
                source_tables=["inventory", "pricing", "desk"],
            )

        by_size = [
            SizeStock(
                size=r["size"],
                qty=r["qty"],
                location=r["location"],
                in_stock=r["qty"] > 0,
            )
            for r in stock_rows
        ]

        pricing: Pricing | None = None
        pricing_warning: str | None = None
        if price_row is not None:
            unit_cost = _money(price_row["unit_cost"])
            list_price = _money(price_row["list_price"])
            # Same number, two decisions: how profitable the SKU is, and how deep
            # a discount can go before the sale prices at cost.
            margin_pct = (
                round((list_price - unit_cost) / list_price * 100, 1) if list_price else None
            )
            pricing = Pricing(
                unit_cost=unit_cost,
                list_price=list_price,
                unit_gross_margin=_money(list_price - unit_cost),
                gross_margin_pct=margin_pct,
                max_discount_pct_before_selling_at_cost=margin_pct,
            )
        else:
            pricing_warning = f"No pricing row for {sku!r}; cost and list price are unknown."

        requested: RequestedSizeStock | None = None
        if size is not None:
            match = next((s for s in by_size if s.size == size), None)
            if match is None:
                requested = RequestedSizeStock(
                    size=size,
                    exists=False,
                    available_sizes=[s.size for s in by_size],
                )
            else:
                requested = RequestedSizeStock(
                    size=size,
                    exists=True,
                    qty_on_hand=match.qty,
                    location=match.location,
                    qty_requested=qty,
                    shortfall=None if qty is None else max(0, qty - match.qty),
                    can_fulfill_from_stock=None if qty is None else match.qty >= qty,
                )

        quote: QuoteMath | None = None
        if qty is not None and pricing is not None:
            quote = QuoteMath(
                qty=qty,
                revenue_at_list=_money(pricing.list_price * qty),
                total_cost=_money(pricing.unit_cost * qty),
                gross_margin_at_list=_money((pricing.list_price - pricing.unit_cost) * qty),
                price_floor_per_unit=pricing.unit_cost,
                note="A quote moves no cash; nothing here debits cash_accounts.",
            )

        return InventoryPricingReport(
            as_of=today,
            sku=sku,
            found=True,
            product_name=stock_rows[0]["name"] if stock_rows else None,
            stock_by_size=by_size,
            total_units_all_sizes=sum(r["qty"] for r in stock_rows),
            pricing=pricing,
            pricing_warning=pricing_warning,
            requested_size=requested,
            quote=quote,
            source_tables=["inventory", "pricing", "desk"],
        )


# --------------------------------------------------------------------------
# tool 4 - the ticket board
# --------------------------------------------------------------------------


@mcp.tool
def get_open_tickets(
    status: Annotated[
        str,
        Field(description="Ticket status to list, e.g. 'open'. Pass 'all' for every ticket."),
    ] = "open",
) -> TicketBoardReport:
    """Get the ticket board: what work is on the desk and what each ticket points at.

    The Boss agent starts here. Each ticket carries the foreign keys that say
    which specialist is needed - invoice_id means an accounting problem, lease_id
    a facilities problem, sku/size/qty an inventory or pricing problem.

    Returns a TicketBoardReport. Ticket notes are the requester's claim, not a
    verified fact; confirm them against the invoice, lease, or inventory tables.
    """
    with closing(_connect()) as conn:
        today = _desk_date(conn)
        sql = "SELECT * FROM tickets"
        params: list[Any] = []
        if status != "all":
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY id"

        tickets = [
            TicketRecord(
                ticket_id=r["id"],
                type=r["type"],
                requester=r["requester"],
                subject=r["subject"],
                sku=r["sku"],
                size=r["size"],
                qty=r["qty"],
                lease_id=r["lease_id"],
                invoice_id=r["invoice_id"],
                status=r["status"],
                notes=r["notes"],
                created_at=r["created_at"],
            )
            for r in conn.execute(sql, params).fetchall()
        ]
        return TicketBoardReport(
            as_of=today,
            ticket_count=len(tickets),
            tickets=tickets,
            source_tables=["tickets", "desk"],
        )


# --------------------------------------------------------------------------
# tool 5 - cash position
# --------------------------------------------------------------------------


@mcp.tool
def get_cash_balance(
    account: Annotated[str, Field(description="Account name, e.g. 'checking'.")] = "checking",
) -> CashBalanceReport:
    """Get the current cash balance and everything already disbursed from it.

    Call this before recommending any payment. The balance is the hard ceiling
    on spending, and payments_recorded shows what has already gone out, so an
    obligation never gets paid twice.

    Returns a CashBalanceReport, with found=False for an unknown account.
    """
    with closing(_connect()) as conn:
        today = _desk_date(conn)
        row = conn.execute(
            "SELECT name, balance, date FROM cash_accounts WHERE name = ?", (account,)
        ).fetchone()
        if row is None:
            return CashBalanceReport(
                as_of=today,
                found=False,
                not_found_reason=f"No cash account named {account!r}.",
                available_accounts=[
                    r["name"] for r in conn.execute("SELECT name FROM cash_accounts ORDER BY name")
                ],
                source_tables=["cash_accounts", "desk"],
            )

        paid = [
            PaymentRecord(
                payment_id=p["id"],
                kind=p["kind"],
                ref_id=p["ref_id"],
                amount=_money(p["amount"]),
                account=p["account"],
                paid_at=p["paid_at"],
                approved_by=p["approved_by"],
            )
            for p in conn.execute(
                "SELECT * FROM payments WHERE account = ? ORDER BY id", (account,)
            ).fetchall()
        ]
        return CashBalanceReport(
            as_of=today,
            found=True,
            account=row["name"],
            balance=_money(row["balance"]),
            balance_as_of=row["date"],
            payments_recorded=paid,
            total_disbursed=_money(sum(p.amount for p in paid)),
            source_tables=["cash_accounts", "payments", "desk"],
        )


# --------------------------------------------------------------------------
# tool 6 - draft a voucher (prepares only, moves no money)
# --------------------------------------------------------------------------


def _obligation(conn: sqlite3.Connection, kind: str, ref_id: int) -> dict[str, Any] | None:
    """Read the amount and payee for an obligation. Agents never supply these."""
    if kind == "invoice":
        row = conn.execute(
            "SELECT i.id, i.amount, i.due_date, i.status, i.description, v.name AS payee "
            "FROM invoices i LEFT JOIN vendors v ON v.id = i.vendor_id WHERE i.id = ?",
            (ref_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "amount": _money(row["amount"]),
            "payee": row["payee"],
            "due_date": row["due_date"],
            "status": row["status"],
            "memo": row["description"],
        }
    row = conn.execute(
        "SELECT id, space_name, landlord, monthly_rent, next_due FROM leases WHERE id = ?",
        (ref_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "amount": _money(row["monthly_rent"]),
        "payee": row["landlord"],
        "due_date": row["next_due"],
        "status": "open",
        "memo": f"Monthly rent for {row['space_name']}",
    }


@mcp.tool
def draft_payment_voucher(
    kind: Annotated[
        Literal["invoice", "rent"],
        Field(description="'invoice' settles an invoices row; 'rent' settles a leases row."),
    ],
    ref_id: Annotated[
        int, Field(description="invoices.id when kind='invoice', leases.id when kind='rent'.")
    ],
    account: Annotated[str, Field(description="Account to draw on, e.g. 'checking'.")] = "checking",
) -> PaymentVoucher:
    """Draft a payment voucher for a human to approve. Writes nothing, moves no cash.

    This is how an agent recommends a disbursement. The amount is read from the
    invoice or lease - an agent cannot propose a number of its own. The voucher
    comes back with requires_human_approval=True and status
    'awaiting_human_approval'; only a human calling record_approved_payment can
    turn it into money leaving the account.

    Returns a PaymentVoucher, with drafted=False when the obligation does not
    exist, is already paid, or would overdraw the account.
    """
    with closing(_connect()) as conn:
        today = _desk_date(conn)
        voucher_id = f"VCH-{kind.upper()}-{ref_id}"
        base = dict(
            as_of=today,
            kind=kind,
            ref_id=ref_id,
            account=account,
            source_tables=["invoices", "vendors", "leases", "payments", "cash_accounts", "desk"],
        )

        ob = _obligation(conn, kind, ref_id)
        if ob is None:
            table = "invoices" if kind == "invoice" else "leases"
            return PaymentVoucher(
                drafted=False,
                blocked_reason=f"No {kind} with id {ref_id} exists in the {table} table.",
                **base,
            )

        already = conn.execute(
            "SELECT id, amount, approved_by FROM payments WHERE kind = ? AND ref_id = ?",
            (kind, ref_id),
        ).fetchone()
        if already is not None:
            return PaymentVoucher(
                drafted=False,
                blocked_reason=(
                    f"Already paid: payment {already['id']} of ${_money(already['amount']):.2f} "
                    f"was approved by {already['approved_by']}. Do not pay it twice."
                ),
                voucher_id=voucher_id,
                payee=ob["payee"],
                amount=ob["amount"],
                due_date=ob["due_date"],
                **base,
            )

        if kind == "invoice" and ob["status"] == "paid":
            return PaymentVoucher(
                drafted=False,
                blocked_reason=f"Invoice {ref_id} is already marked paid.",
                voucher_id=voucher_id,
                payee=ob["payee"],
                amount=ob["amount"],
                **base,
            )

        cash = conn.execute(
            "SELECT balance FROM cash_accounts WHERE name = ?", (account,)
        ).fetchone()
        if cash is None:
            return PaymentVoucher(
                drafted=False,
                blocked_reason=f"No cash account named {account!r}.",
                voucher_id=voucher_id,
                **base,
            )

        balance = _money(cash["balance"])
        after = _money(balance - ob["amount"])
        sufficient = after >= 0

        return PaymentVoucher(
            drafted=sufficient,
            blocked_reason=(
                None
                if sufficient
                else (
                    f"Insufficient funds: {account} holds ${balance:.2f} but the obligation "
                    f"is ${ob['amount']:.2f}. Escalate to a human instead of paying partially."
                )
            ),
            voucher_id=voucher_id,
            payee=ob["payee"],
            amount=ob["amount"],
            due_date=ob["due_date"],
            days_past_due=_days_between(ob["due_date"], today),
            memo=ob["memo"],
            balance_before=balance,
            balance_after_if_approved=after,
            sufficient_funds=sufficient,
            requires_human_approval=True,
            status="awaiting_human_approval",
            **base,
        )


# --------------------------------------------------------------------------
# tool 7 - record a HUMAN-APPROVED payment (the only tool that moves cash)
# --------------------------------------------------------------------------

#: Names that must never appear in payments.approved_by. An agent approving its
#: own voucher would defeat the entire human-in-the-loop control.
AGENT_APPROVER_BLOCKLIST = {
    "boss",
    "inventory",
    "accounting",
    "facilities",
    "customer_service",
    "customer service",
    "agent",
    "ai",
    "assistant",
    "system",
    "auto",
    "automatic",
    "bot",
    "llm",
    "model",
    "gpt",
    "claude",
}


@mcp.tool
def record_approved_payment(
    kind: Annotated[
        Literal["invoice", "rent"],
        Field(description="'invoice' settles an invoices row; 'rent' settles a leases row."),
    ],
    ref_id: Annotated[
        int, Field(description="invoices.id when kind='invoice', leases.id when kind='rent'.")
    ],
    amount: Annotated[
        float,
        Field(
            description="Amount approved. Must equal the obligation exactly, or the payment is refused."
        ),
    ],
    approved_by: Annotated[
        str,
        Field(
            description="Name of the HUMAN who approved this disbursement. Agent names are rejected."
        ),
    ],
    account: Annotated[str, Field(description="Account to debit, e.g. 'checking'.")] = "checking",
) -> PaymentResult:
    """Record a disbursement a HUMAN has already approved. This is the only tool that moves cash.

    An agent must never call this on its own authority. It writes a payments row,
    debits the account, and marks the obligation settled, all in one transaction.

    Four guardrails refuse the write outright: approved_by must name a human
    (agent names are blocked), the amount must match the obligation to the cent,
    the obligation must not already be paid, and the balance must cover it. Each
    refusal returns recorded=False with the reason - nothing is written.
    """
    with closing(_connect_rw()) as conn:
        today = _desk_date(conn)
        base = dict(
            as_of=today,
            kind=kind,
            ref_id=ref_id,
            account=account,
            source_tables=["payments", "cash_accounts", "invoices", "leases", "desk"],
        )

        approver = (approved_by or "").strip()
        if not approver:
            return PaymentResult(
                recorded=False,
                rejected_reason="No approver supplied. A named human must approve every disbursement.",
                **base,
            )
        if approver.lower().replace("-", "_") in AGENT_APPROVER_BLOCKLIST:
            return PaymentResult(
                recorded=False,
                rejected_reason=(
                    f"{approver!r} is an agent, not a human approver. Agents prepare vouchers; "
                    "a human approves them."
                ),
                **base,
            )

        ob = _obligation(conn, kind, ref_id)
        if ob is None:
            table = "invoices" if kind == "invoice" else "leases"
            return PaymentResult(
                recorded=False,
                rejected_reason=f"No {kind} with id {ref_id} exists in the {table} table.",
                **base,
            )

        if _money(amount) != ob["amount"]:
            return PaymentResult(
                recorded=False,
                rejected_reason=(
                    f"Amount mismatch: approved ${_money(amount):.2f} but the {kind} is "
                    f"${ob['amount']:.2f}. The ledger only accepts the exact obligation."
                ),
                **base,
            )

        dup = conn.execute(
            "SELECT id FROM payments WHERE kind = ? AND ref_id = ?", (kind, ref_id)
        ).fetchone()
        if dup is not None:
            return PaymentResult(
                recorded=False,
                rejected_reason=f"Already paid by payment {dup['id']}. Refusing to double-pay.",
                **base,
            )

        cash = conn.execute(
            "SELECT balance FROM cash_accounts WHERE name = ?", (account,)
        ).fetchone()
        if cash is None:
            return PaymentResult(
                recorded=False,
                rejected_reason=f"No cash account named {account!r}.",
                **base,
            )

        before = _money(cash["balance"])
        after = _money(before - ob["amount"])
        if after < 0:
            return PaymentResult(
                recorded=False,
                rejected_reason=(
                    f"Insufficient funds: ${before:.2f} available, ${ob['amount']:.2f} required."
                ),
                balance_before=before,
                **base,
            )

        with conn:  # one transaction: ledger, balance, and obligation move together
            cur = conn.execute(
                "INSERT INTO payments (kind, ref_id, amount, account, paid_at, approved_by) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (kind, ref_id, ob["amount"], account, today, approver),
            )
            conn.execute(
                "UPDATE cash_accounts SET balance = ?, date = ? WHERE name = ?",
                (after, today, account),
            )
            obligation_status = "paid"
            if kind == "invoice":
                conn.execute("UPDATE invoices SET status = 'paid' WHERE id = ?", (ref_id,))
            else:
                obligation_status = "rent_paid"

        return PaymentResult(
            recorded=True,
            payment_id=cur.lastrowid,
            amount=ob["amount"],
            approved_by=approver,
            balance_before=before,
            balance_after=after,
            obligation_status=obligation_status,
            **base,
        )


# --------------------------------------------------------------------------
# tool 8 - move a ticket along
# --------------------------------------------------------------------------

ALLOWED_TICKET_STATUSES = ["open", "in_progress", "awaiting_approval", "resolved", "blocked"]


@mcp.tool
def update_ticket_status(
    ticket_id: Annotated[int, Field(description="Ticket to update, e.g. 101.")],
    status: Annotated[
        Literal["open", "in_progress", "awaiting_approval", "resolved", "blocked"],
        Field(description="New status. 'awaiting_approval' means a human still has to act."),
    ],
    note: Annotated[
        str | None,
        Field(
            description="What was decided and why. Appended to the ticket's existing notes, "
            "never replacing them."
        ),
    ] = None,
) -> TicketUpdateResult:
    """Update a ticket's status, appending a note explaining the change.

    Use this to move work along the board. Existing notes are preserved - the new
    note is appended - so the ticket keeps its history. Marking a ticket
    'resolved' is a claim the team will be audited on, so only do it once the
    underlying obligation has actually been settled or the customer answered.

    Returns a TicketUpdateResult, with updated=False for an unknown ticket.
    """
    with closing(_connect_rw()) as conn:
        today = _desk_date(conn)
        base = dict(
            as_of=today,
            ticket_id=ticket_id,
            allowed_statuses=ALLOWED_TICKET_STATUSES,
            source_tables=["tickets", "desk"],
        )

        row = conn.execute(
            "SELECT status, notes FROM tickets WHERE id = ?", (ticket_id,)
        ).fetchone()
        if row is None:
            return TicketUpdateResult(
                updated=False,
                rejected_reason=f"No ticket with id {ticket_id} exists.",
                **base,
            )

        previous = row["status"]
        notes = row["notes"]
        if note:
            stamped = f"[{today}] {note}"
            notes = f"{notes}\n{stamped}" if notes else stamped

        with conn:
            conn.execute(
                "UPDATE tickets SET status = ?, notes = ? WHERE id = ?",
                (status, notes, ticket_id),
            )

        return TicketUpdateResult(
            updated=True,
            previous_status=previous,
            new_status=status,
            notes=notes,
            **base,
        )


# --------------------------------------------------------------------------
# entrypoint
# --------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Campus Customs operations MCP server")
    parser.add_argument("--http", action="store_true", help="serve over HTTP instead of stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8003)
    args = parser.parse_args()

    if args.http:
        mcp.run(transport="http", host=args.host, port=args.port)
    else:
        mcp.run()


if __name__ == "__main__":
    main()
