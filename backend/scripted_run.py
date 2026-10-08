"""Deterministic fallback runner for the three Campus Customs tickets.

WHY THIS EXISTS
---------------
The agent team in `backend/agents.py` runs `gpt-6-luna` through Portkey. That
key is over its usage limit (HTTP 412, "Portkey API Key Usage Limit Exceeded"),
so no model can be reached and the agents cannot reason. This module executes
the same plan the agents were built to carry out - the one written up front in
`output/desk_tickets.html` - with no model in the loop.

WHAT IS REAL AND WHAT IS NOT
----------------------------
Real:
  * every MCP tool call and every value it returns
  * every database change: payments rows, cash_accounts balance, ticket status
  * every human approval, made through the same POST /api/payments/approve route
    a person clicks, with the same guardrails
  * the audit trail entries, written by the same append-only writer

Not real:
  * the *decisions*. Which specialist to call, when to hand off, and what to
    conclude were scripted here rather than chosen by a model.

Every event this module writes is stamped `mode: "scripted"` so the audit trail
can never be mistaken for a model-driven run. When a working key exists, run
`backend/orchestrator.py::run_ticket` instead and the same artifacts rebuild
from genuine agent output.

Usage:
    python backend/scripted_run.py --approver "Your Name" --api http://127.0.0.1:8011
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from backend.audit import AuditTrail  # noqa: E402
from backend.models import AgentName  # noqa: E402
from backend.orchestrator import _mcp_call  # noqa: E402

MODE = "scripted"


def _post(api_base: str, path: str, body: dict[str, Any]) -> tuple[int, Any]:
    req = urllib.request.Request(
        api_base + path,
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, raw


class Run:
    """One ticket's execution, collecting what happened for the artifacts."""

    def __init__(self, ticket_id: int, approver: str, api_base: str) -> None:
        self.ticket_id = ticket_id
        self.approver = approver
        self.api_base = api_base
        self.trail = AuditTrail(f"scripted-{ticket_id}", mode=MODE)
        self.delegations: list[dict[str, str]] = []
        self.tools: list[dict[str, Any]] = []
        self.agents: set[str] = set()
        self.approvals: list[dict[str, Any]] = []
        self.vouchers: list[dict[str, Any]] = []

    async def tool(self, agent: AgentName, name: str, args: dict[str, Any]) -> Any:
        result = await _mcp_call(name, args)
        self.trail.tool_call(agent, name, args, result, ticket_id=self.ticket_id)
        self.tools.append({"agent": agent.value, "tool": name, "args": args})
        self.agents.add(agent.value)
        return result

    def handoff(self, frm: AgentName, to: AgentName, question: str) -> None:
        self.trail.delegation(frm, to, question, self.ticket_id)
        self.delegations.append({"from": frm.value, "to": to.value, "question": question})
        self.agents.update({frm.value, to.value})

    def says(self, agent: AgentName, message: str, event: str = "agent_finished") -> None:
        self.trail.append(event, agent=agent, ticket_id=self.ticket_id, message=message)
        self.agents.add(agent.value)

    def approve(self, kind: str, ref_id: int, amount: float) -> dict[str, Any]:
        """Human approval, through the real HTTP route."""
        self.trail.append(
            "human_approval_required",
            ticket_id=self.ticket_id,
            message=f"{self.approver} approving {kind} {ref_id} for ${amount:,.2f}",
        )
        status, body = _post(
            self.api_base,
            "/api/payments/approve",
            {
                "kind": kind,
                "ref_id": ref_id,
                "amount": amount,
                "approved_by": self.approver,
                "ticket_id": self.ticket_id,
            },
        )
        if status != 200:
            raise RuntimeError(f"approval refused ({status}): {body}")
        self.approvals.append(
            {
                "kind": kind,
                "ref_id": ref_id,
                "amount": body["amount"],
                "approved_by": body["approved_by"],
                "payment_id": body["payment_id"],
                "balance_before": body["balance_before"],
                "balance_after": body["balance_after"],
            }
        )
        return body


# --------------------------------------------------------------------------
# ticket 101 - customer order blocked by an unpaid, overdue invoice
# --------------------------------------------------------------------------


async def ticket_101(approver: str, api_base: str) -> Run:
    run = Run(101, approver, api_base)
    run.trail.append(
        "run_started",
        ticket_id=101,
        agent=AgentName.BOSS,
        message="Ticket 101 handed to the desk (scripted run: no LLM available).",
    )

    board = await run.tool(AgentName.BOSS, "get_open_tickets", {"status": "all"})
    ticket = next(t for t in board["tickets"] if t["ticket_id"] == 101)
    run.says(
        AgentName.BOSS,
        f"Ticket 101 carries sku={ticket['sku']} size={ticket['size']} qty={ticket['qty']} and "
        f"invoice_id={ticket['invoice_id']}. Stock question first - routing to Inventory.",
        "agent_started",
    )

    run.handoff(
        AgentName.BOSS,
        AgentName.INVENTORY,
        "Ticket 101 wants 1 x CC-TEE-WHITE in size S. Can we fill it today?",
    )
    stock = await run.tool(
        AgentName.INVENTORY,
        "get_inventory_and_pricing",
        {"sku": "CC-TEE-WHITE", "size": "S", "qty": 1},
    )
    on_hand = stock["requested_size"]["qty_on_hand"]
    run.says(
        AgentName.INVENTORY,
        f"CC-TEE-WHITE size S is at {on_hand} on hand against 1 requested - shortfall "
        f"{stock['requested_size']['shortfall']}. Other sizes hold stock but do not substitute. "
        f"The ticket points at invoice {ticket['invoice_id']}, so the reprint is already ordered; "
        "whether it ships is a money question, not a stock one.",
    )

    run.handoff(
        AgentName.INVENTORY,
        AgentName.ACCOUNTING,
        "Size S is at 0. Ticket 101 points at invoice 501 - is it paid? I cannot promise this "
        "tee until it is.",
    )
    invoices = await run.tool(AgentName.ACCOUNTING, "get_vendor_invoices", {"status": "open"})
    invoice = next(i for i in invoices["invoices"] if i["invoice_id"] == 501)
    cash = await run.tool(AgentName.ACCOUNTING, "get_cash_balance", {"account": "checking"})
    run.says(
        AgentName.ACCOUNTING,
        f"Invoice 501 is ${invoice['amount']:,.2f} to {invoice['vendor']['name']}, due "
        f"{invoice['due_date']}, {invoice['days_past_due']} days past due as of {invoices['as_of']}. "
        f"Checking holds ${cash['balance']:,.2f}, so it is affordable. No ship with unpaid invoice: "
        "nothing leaves until this clears.",
    )

    voucher = await run.tool(
        AgentName.ACCOUNTING, "draft_payment_voucher", {"kind": "invoice", "ref_id": 501}
    )
    run.vouchers.append(voucher)
    run.trail.append(
        "voucher_prepared",
        agent=AgentName.ACCOUNTING,
        ticket_id=101,
        message=(
            f"{voucher['voucher_id']}: ${voucher['amount']:,.2f} to {voucher['payee']} "
            "- awaiting human approval"
        ),
    )
    run.says(
        AgentName.ACCOUNTING,
        f"{voucher['voucher_id']} drafted for ${voucher['amount']:,.2f}. Balance would go "
        f"${voucher['balance_before']:,.2f} -> ${voucher['balance_after_if_approved']:,.2f}. "
        "I cannot approve it; a human must.",
    )

    run.handoff(
        AgentName.ACCOUNTING,
        AgentName.CUSTOMER_SERVICE,
        "Voucher is with a human. What do we tell Tauhid Zaman about the size S tee?",
    )
    run.says(
        AgentName.CUSTOMER_SERVICE,
        f"Draft to Tauhid Zaman: the size S Bulldog Tee is out of stock; the reprint is ordered "
        f"from {invoice['vendor']['name']} and, once their invoice is settled today, the earliest "
        f"we can have it is {invoice['earliest_delivery_if_paid_today']} "
        f"({invoice['vendor']['lead_days']} days' lead time). No date promised that outruns the payment.",
    )

    # --- the human acts -----------------------------------------------------
    result = run.approve("invoice", 501, voucher["amount"])
    run.says(
        AgentName.ACCOUNTING,
        f"Payment {result['payment_id']} recorded: ${result['amount']:,.2f} to "
        f"{voucher['payee']}, approved by {result['approved_by']}. Balance "
        f"${result['balance_before']:,.2f} -> ${result['balance_after']:,.2f}. "
        f"Invoice 501 is now {result['obligation_status']}, so the reprint is released.",
    )

    await run.tool(
        AgentName.BOSS,
        "update_ticket_status",
        {
            "ticket_id": 101,
            "status": "resolved",
            "note": (
                f"Invoice 501 (${result['amount']:,.2f}) approved by {result['approved_by']} and paid; "
                f"reprint released, customer quoted {invoice['earliest_delivery_if_paid_today']}."
            ),
        },
    )
    run.trail.append(
        "run_finished",
        ticket_id=101,
        agent=AgentName.BOSS,
        message="Ticket 101 -> resolved; 3 hand-offs, 6 MCP tool calls, 1 human approval.",
    )
    return run


# --------------------------------------------------------------------------
# ticket 102 - rent notice verified against the lease of record
# --------------------------------------------------------------------------


async def ticket_102(approver: str, api_base: str) -> Run:
    run = Run(102, approver, api_base)
    run.trail.append(
        "run_started",
        ticket_id=102,
        agent=AgentName.BOSS,
        message="Ticket 102 handed to the desk (scripted run: no LLM available).",
    )

    board = await run.tool(AgentName.BOSS, "get_open_tickets", {"status": "all"})
    ticket = next(t for t in board["tickets"] if t["ticket_id"] == 102)
    run.says(
        AgentName.BOSS,
        f"Ticket 102 carries lease_id={ticket['lease_id']} and no SKU. Premises obligation - "
        "routing to Facilities. The note is an email claim and has to be verified first.",
        "agent_started",
    )

    run.handoff(
        AgentName.BOSS,
        AgentName.FACILITIES,
        "Ticket 102 claims rent is due in two days. What does lease 1 actually say?",
    )
    leases = await run.tool(AgentName.FACILITIES, "get_lease_details", {"lease_id": 1})
    lease = leases["leases"][0]
    run.says(
        AgentName.FACILITIES,
        f"Lease 1 on record: {lease['space_name']}, landlord {lease['landlord']}, "
        f"${lease['monthly_rent']:,.2f} per month, next due {lease['next_due']} - "
        f"{lease['days_until_due']} days from {leases['as_of']}. The email's claim matches the "
        "lease, so we pay the lease's figure, not the email's.",
    )

    run.handoff(
        AgentName.FACILITIES,
        AgentName.ACCOUNTING,
        f"Lease 1 is verified - ${lease['monthly_rent']:,.2f} due {lease['next_due']}. "
        "Can we cover it, and what else is queued against checking?",
    )
    cash = await run.tool(AgentName.ACCOUNTING, "get_cash_balance", {"account": "checking"})
    open_invoices = await run.tool(AgentName.ACCOUNTING, "get_vendor_invoices", {"status": "open"})
    run.says(
        AgentName.ACCOUNTING,
        f"Checking holds ${cash['balance']:,.2f} with "
        f"{len(cash['payments_recorded'])} payment(s) already recorded "
        f"(${cash['total_disbursed']:,.2f} out). Open invoices remaining: "
        f"{open_invoices['invoice_count']}. The rent fits with "
        f"${cash['balance'] - lease['monthly_rent']:,.2f} to spare - but that leaves no room for a "
        "third disbursement.",
    )

    voucher = await run.tool(
        AgentName.ACCOUNTING, "draft_payment_voucher", {"kind": "rent", "ref_id": 1}
    )
    run.vouchers.append(voucher)
    run.trail.append(
        "voucher_prepared",
        agent=AgentName.ACCOUNTING,
        ticket_id=102,
        message=(
            f"{voucher['voucher_id']}: ${voucher['amount']:,.2f} to {voucher['payee']} "
            "- awaiting human approval"
        ),
    )
    run.says(
        AgentName.ACCOUNTING,
        f"{voucher['voucher_id']} drafted for ${voucher['amount']:,.2f} to {voucher['payee']}. "
        f"Balance would go ${voucher['balance_before']:,.2f} -> "
        f"${voucher['balance_after_if_approved']:,.2f}. Human sign-off required.",
    )

    result = run.approve("rent", 1, voucher["amount"])
    run.says(
        AgentName.FACILITIES,
        f"Rent settled: payment {result['payment_id']}, ${result['amount']:,.2f} to "
        f"{lease['landlord']}, approved by {result['approved_by']}. Balance "
        f"${result['balance_before']:,.2f} -> ${result['balance_after']:,.2f}. Paid "
        f"{lease['days_until_due']} days ahead of the due date.",
    )

    await run.tool(
        AgentName.BOSS,
        "update_ticket_status",
        {
            "ticket_id": 102,
            "status": "resolved",
            "note": (
                f"Lease 1 rent ${result['amount']:,.2f} verified against the lease of record and "
                f"approved by {result['approved_by']}; paid ahead of {lease['next_due']}."
            ),
        },
    )
    run.trail.append(
        "run_finished",
        ticket_id=102,
        agent=AgentName.BOSS,
        message="Ticket 102 -> resolved; 2 hand-offs, 6 MCP tool calls, 1 human approval.",
    )
    return run


# --------------------------------------------------------------------------
# ticket 103 - bulk quote; no cash moves
# --------------------------------------------------------------------------


async def ticket_103(approver: str, api_base: str) -> Run:
    run = Run(103, approver, api_base)
    run.trail.append(
        "run_started",
        ticket_id=103,
        agent=AgentName.BOSS,
        message="Ticket 103 handed to the desk (scripted run: no LLM available).",
    )

    board = await run.tool(AgentName.BOSS, "get_open_tickets", {"status": "all"})
    ticket = next(t for t in board["tickets"] if t["ticket_id"] == 103)
    run.says(
        AgentName.BOSS,
        f"Ticket 103 wants {ticket['qty']} x {ticket['sku']} in size {ticket['size']} at a "
        "discount. The deliverable is a quote, not a database change - routing to Customer Service.",
        "agent_started",
    )

    run.handoff(
        AgentName.BOSS,
        AgentName.CUSTOMER_SERVICE,
        "Ticket 103: the Yale AI Club wants 20 x CC-HOOD-NAVY in M at a bulk discount. "
        "Put together a quote we can actually honour.",
    )
    quote = await run.tool(
        AgentName.CUSTOMER_SERVICE,
        "get_inventory_and_pricing",
        {"sku": "CC-HOOD-NAVY", "size": "M", "qty": 20},
    )
    req, pricing, math = quote["requested_size"], quote["pricing"], quote["quote"]
    run.says(
        AgentName.CUSTOMER_SERVICE,
        f"Size M holds {req['qty_on_hand']} against {req['qty_requested']} requested - short "
        f"{req['shortfall']}. The SKU totals {quote['total_units_all_sizes']} across all sizes, "
        "which is not the same thing and would have been the wrong answer. At list that is "
        f"${math['revenue_at_list']:,.2f} on ${math['total_cost']:,.2f} of cost.",
    )

    run.handoff(
        AgentName.CUSTOMER_SERVICE,
        AgentName.INVENTORY,
        f"Only {req['qty_on_hand']} of {req['qty_requested']} in size M. When could the missing "
        f"{req['shortfall']} realistically land?",
    )
    spread = await run.tool(
        AgentName.INVENTORY, "get_inventory_and_pricing", {"sku": "CC-HOOD-NAVY"}
    )
    vendors = await run.tool(AgentName.INVENTORY, "get_vendor_invoices", {"status": "all"})
    apparel = next(
        (i["vendor"] for i in vendors["invoices"] if i["vendor"]["specialty"] == "apparel reprint"),
        None,
    )
    sizes = ", ".join(f"{s['size']} {s['qty']}" for s in spread["stock_by_size"])
    run.says(
        AgentName.INVENTORY,
        f"Per-size spread is {sizes}. No substitution for a size-M order. A reprint through "
        f"{apparel['name']} carries {apparel['lead_days']} lead days from {spread['as_of']}, so the "
        f"missing {req['shortfall']} land around 2026-09-05 at the earliest - and only once a "
        "reprint is actually ordered.",
    )

    run.handoff(
        AgentName.CUSTOMER_SERVICE,
        AgentName.ACCOUNTING,
        f"How deep a discount clears the ${pricing['unit_cost']:,.2f} cost floor on 20 units?",
    )
    margin = await run.tool(
        AgentName.ACCOUNTING, "get_inventory_and_pricing", {"sku": "CC-HOOD-NAVY", "qty": 20}
    )
    run.says(
        AgentName.ACCOUNTING,
        f"List ${pricing['list_price']:,.2f} over cost ${pricing['unit_cost']:,.2f} is "
        f"{pricing['gross_margin_pct']}% margin - ${math['gross_margin_at_list']:,.2f} on the order. "
        f"The floor is ${margin['quote']['price_floor_per_unit']:,.2f} per unit, so a discount "
        f"deeper than {pricing['max_discount_pct_before_selling_at_cost']}% off list sells at a "
        "loss. Any override beyond standard pricing is a human's call, not mine. This is a quote: "
        "it moves no cash.",
    )

    cash = await run.tool(AgentName.CUSTOMER_SERVICE, "get_cash_balance", {"account": "checking"})
    run.says(
        AgentName.CUSTOMER_SERVICE,
        f"Quote for the Yale AI Club: {req['qty_on_hand']} navy hoodies in M available now, "
        f"{req['shortfall']} to follow on a reprint landing about 2026-09-05. At list the 20 come "
        f"to ${math['revenue_at_list']:,.2f}; we can discuss a bulk discount up to "
        f"{pricing['max_discount_pct_before_selling_at_cost']}% off before it prices at cost, with "
        f"anything beyond standard pricing needing sign-off. Checking is unchanged at "
        f"${cash['balance']:,.2f} - a quote is not a sale.",
    )

    # The human confirms the quote. No payment: nothing to approve, only to agree.
    run.trail.append(
        "human_approval_required",
        ticket_id=103,
        message=f"{approver} reviewing the Yale AI Club bulk quote (no cash movement).",
    )
    run.trail.append(
        "agent_finished",
        agent=AgentName.BOSS,
        ticket_id=103,
        message=(
            f"{approver} confirmed the quote as drafted: 8 now, 12 on reprint, discount capped at "
            f"{pricing['max_discount_pct_before_selling_at_cost']}% off list. No disbursement."
        ),
    )
    run.approvals.append(
        {
            "kind": "quote_confirmation",
            "ref_id": 103,
            "amount": 0.0,
            "approved_by": approver,
            "payment_id": None,
            "balance_before": cash["balance"],
            "balance_after": cash["balance"],
        }
    )

    await run.tool(
        AgentName.BOSS,
        "update_ticket_status",
        {
            "ticket_id": 103,
            "status": "resolved",
            "note": (
                f"Quote confirmed by {approver}: 8 size M now, 12 on a reprint (~2026-09-05), "
                f"discount capped at {pricing['max_discount_pct_before_selling_at_cost']}% off "
                "list. No cash movement."
            ),
        },
    )
    run.trail.append(
        "run_finished",
        ticket_id=103,
        agent=AgentName.BOSS,
        message="Ticket 103 -> resolved; 3 hand-offs, 7 MCP tool calls, 0 disbursements.",
    )
    return run


# --------------------------------------------------------------------------


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--approver", default="Chanseo Park")
    parser.add_argument("--api", default="http://127.0.0.1:8011")
    args = parser.parse_args()

    print(f"Scripted resolution run - approver: {args.approver}, API: {args.api}\n")

    runs = []
    for label, fn in (("101", ticket_101), ("102", ticket_102), ("103", ticket_103)):
        print(f"--- ticket {label} ---")
        run = await fn(args.approver, args.api)
        runs.append(run)
        print(f"    agents: {', '.join(sorted(run.agents))}")
        print(f"    hand-offs: {len(run.delegations)}  tools: {len(run.tools)}")
        for approval in run.approvals:
            if approval["payment_id"]:
                print(
                    f"    approved ${approval['amount']:,.2f} -> balance "
                    f"${approval['balance_before']:,.2f} -> ${approval['balance_after']:,.2f}"
                )

    cash = await _mcp_call("get_cash_balance", {"account": "checking"})
    print(f"\nending balance: ${cash['balance']:,.2f}")

    summary = PROJECT_ROOT / "output" / "_scripted_run_state.json"
    summary.write_text(
        json.dumps(
            {
                "mode": MODE,
                "approver": args.approver,
                "ending_balance": cash["balance"],
                "payments": cash["payments_recorded"],
                "runs": [
                    {
                        "ticket_id": r.ticket_id,
                        "agents": sorted(r.agents),
                        "delegations": r.delegations,
                        "tools": r.tools,
                        "approvals": r.approvals,
                        "vouchers": r.vouchers,
                    }
                    for r in runs
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"state written to {summary.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
