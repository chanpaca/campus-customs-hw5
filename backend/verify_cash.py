"""Audit the Cash tab in output/desk_tickets.html against the live database.

This does not trust the write-up. It parses the numbers out of the HTML, reads
the same figures out of data/campus_customs_new.db, and fails if they disagree.

    python backend/verify_cash.py

Exits non-zero if any check fails.
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from html.parser import HTMLParser
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DESK_HTML = PROJECT_ROOT / "output" / "desk_tickets.html"
DB = PROJECT_ROOT / "data" / "campus_customs_new.db"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PASS: list[str] = []
FAIL: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(label)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  <- {detail}" if detail else ""))


class CashTabParser(HTMLParser):
    """Pull every table row out of the Cash panel of desk_tickets.html."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_cash_panel = False
        self.depth_in_panel = 0
        self.in_row = False
        self.in_cell = False
        self.cell: list[str] = []
        self.row: list[str] = []
        self.rows: list[list[str]] = []

    def handle_starttag(self, tag, attrs):
        attr = dict(attrs)
        if tag == "section" and attr.get("id") == "panel-cash":
            self.in_cash_panel = True
            self.depth_in_panel = 0
            return
        if not self.in_cash_panel:
            return
        if tag == "section":
            self.depth_in_panel += 1
        elif tag == "tr":
            self.in_row, self.row = True, []
        elif tag in ("td", "th"):
            self.in_cell, self.cell = True, []

    def handle_endtag(self, tag):
        if not self.in_cash_panel:
            return
        if tag == "section":
            if self.depth_in_panel == 0:
                self.in_cash_panel = False
            else:
                self.depth_in_panel -= 1
        elif tag == "tr" and self.in_row:
            self.rows.append(self.row)
            self.in_row = False
        elif tag in ("td", "th") and self.in_cell:
            self.row.append("".join(self.cell).strip())
            self.in_cell = False

    def handle_data(self, data):
        if self.in_cell:
            self.cell.append(data)


def money(text: str) -> float | None:
    """'2,560.00' -> 2560.0 ; '—' -> None."""
    cleaned = re.sub(r"[^\d.\-]", "", text.replace(",", ""))
    if not cleaned or cleaned in {"-", "."}:
        return None
    try:
        return round(float(cleaned), 2)
    except ValueError:
        return None


def _show(path: Path) -> str:
    """Repo-relative when possible, absolute otherwise."""
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def main(argv: list[str] | None = None) -> int:
    args = argparse.ArgumentParser(description="Audit the Cash tab against the database")
    args.add_argument("--html", default=str(DESK_HTML), help="desk_tickets.html to audit")
    args.add_argument("--db", default=str(DB), help="database to audit against")
    opts = args.parse_args(argv)
    html_path, db_path = Path(opts.html), Path(opts.db)

    print(f"Auditing {_show(html_path)} against {_show(db_path)}\n")

    parser = CashTabParser()
    parser.feed(html_path.read_text(encoding="utf-8"))
    rows = [r for r in parser.rows if r]

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    balance = conn.execute(
        "SELECT balance FROM cash_accounts WHERE name = 'checking'"
    ).fetchone()["balance"]
    payments = [dict(r) for r in conn.execute("SELECT * FROM payments ORDER BY id")]
    invoice = conn.execute("SELECT status FROM invoices WHERE id = 501").fetchone()["status"]
    tickets = {r["id"]: r["status"] for r in conn.execute("SELECT id, status FROM tickets")}
    conn.close()

    # ---- the two checks that were asked for --------------------------------
    print("Database truth")
    check("cash_accounts.balance is exactly 160.0", balance == 160.0, f"got {balance!r}")
    check("payments table holds exactly 2 rows", len(payments) == 2, f"got {len(payments)}")

    by_ref = {(p["kind"], p["ref_id"]): p for p in payments}
    inv_pay = by_ref.get(("invoice", 501))
    rent_pay = by_ref.get(("rent", 1))

    check("payment for invoice 501 exists", inv_pay is not None)
    if inv_pay:
        check("  invoice 501 amount is 840.0", inv_pay["amount"] == 840.0, f"got {inv_pay['amount']}")
        check("  invoice 501 debited checking", inv_pay["account"] == "checking")
        check("  invoice 501 approved by a human", inv_pay["approved_by"] == "Chanseo Park",
              inv_pay["approved_by"])
        check("  invoice 501 stamped with the desk date", inv_pay["paid_at"] == "2026-08-31",
              inv_pay["paid_at"])

    check("payment for lease 1 rent exists", rent_pay is not None)
    if rent_pay:
        check("  rent amount is 2400.0", rent_pay["amount"] == 2400.0, f"got {rent_pay['amount']}")
        check("  rent debited checking", rent_pay["account"] == "checking")
        check("  rent approved by a human", rent_pay["approved_by"] == "Chanseo Park",
              rent_pay["approved_by"])

    total = round(sum(p["amount"] for p in payments), 2)
    check("ledger total is 3240.0", total == 3240.0, f"got {total}")
    check("3400.00 - ledger total == balance", round(3400.0 - total, 2) == balance,
          f"3400.00 - {total} = {round(3400.0 - total, 2)} vs balance {balance}")

    # ---- now the HTML has to agree -----------------------------------------
    print("\nHTML Cash tab vs database")
    check("Cash tab parsed", len(rows) > 0, f"{len(rows)} table rows found")

    def find_row(needle: str) -> list[str] | None:
        return next((r for r in rows if needle.lower() in r[0].lower()), None)

    start = find_row("Starting balance")
    t101 = find_row("Ticket 101")
    t102 = find_row("Ticket 102")
    t103 = find_row("Ticket 103")
    ending = find_row("Ending balance")

    if start:
        check("HTML starting balance is 3,400.00", money(start[-1]) == 3400.0, start[-1])
    if t101:
        check("HTML ticket 101 outflow is 840.00", money(t101[2]) == 840.0, t101[2])
        check("HTML balance after 101 is 2,560.00", money(t101[-1]) == 2560.0, t101[-1])
    if t102:
        check("HTML ticket 102 outflow is 2,400.00", money(t102[2]) == 2400.0, t102[2])
        check("HTML balance after 102 is 160.00", money(t102[-1]) == 160.0, t102[-1])
    if t103:
        check("HTML ticket 103 outflow is 0.00", money(t103[2]) == 0.0, t103[2])
        check("HTML balance after 103 is 160.00", money(t103[-1]) == 160.0, t103[-1])
    if ending:
        check("HTML ending balance matches the database", money(ending[-1]) == balance,
              f"html {ending[-1]} vs db {balance}")
        check("HTML total disbursed matches the ledger", money(ending[2]) == total,
              f"html {ending[2]} vs db {total}")

    # the ledger table printed in the Cash tab
    print("\nHTML payments ledger vs database")
    for payment in payments:
        row = next(
            (
                r
                for r in rows
                if r and r[0] == str(payment["id"]) and len(r) >= 7
            ),
            None,
        )
        label = f"payment id {payment['id']} ({payment['kind']} {payment['ref_id']})"
        check(f"{label} listed in the HTML", row is not None)
        if row:
            check(f"  {label} amount matches", money(row[3]) == payment["amount"],
                  f"html {row[3]} vs db {payment['amount']}")
            check(f"  {label} approver matches", row[6] == payment["approved_by"],
                  f"html {row[6]!r} vs db {payment['approved_by']!r}")

    # ---- supporting state --------------------------------------------------
    print("\nSupporting state")
    check("invoice 501 is marked paid", invoice == "paid", invoice)
    check("all three tickets resolved", all(s == "resolved" for s in tickets.values()),
          str(tickets))
    check("no agent name appears as an approver",
          not any(
              p["approved_by"].strip().lower().replace("-", "_")
              in {"boss", "inventory", "accounting", "facilities", "customer_service", "agent", "ai"}
              for p in payments
          ))

    print(f"\n{'=' * 62}\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED: " + "; ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
