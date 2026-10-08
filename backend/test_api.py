"""End-to-end check of every Campus Customs API route.

Start the server first, then run this against it:

    python -m uvicorn main:app --reload --port 8000 --app-dir backend
    python backend/test_api.py --base http://127.0.0.1:8000

It exercises all eight routes and asserts the two that actually change state:

  * POST /api/payments/approve must write a payments row and debit
    cash_accounts - verified by reading /api/cash before and after.
  * POST /api/reset must restore data/campus_customs_new.db from the seed -
    verified by comparing file hashes and re-reading the balance.

Exits non-zero if any check fails.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

# Console on this machine is cp949; keep output ASCII-safe.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SEED_DB = PROJECT_ROOT / "data" / "campus_customs.db"
WORKING_DB = PROJECT_ROOT / "data" / "campus_customs_new.db"

PASS, FAIL = [], []


def call(base: str, method: str, path: str, body: dict | None = None, timeout: int = 120):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        base + path, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, raw


def check(label: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(label)
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f"  <- {detail}" if detail else ""))


def md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--approver", default="Chanseo Park")
    base = parser.parse_args().base
    approver = parser.parse_args().approver

    print(f"Testing {base}\n")

    # Start from a known state.
    print("0. POST /api/reset  (establish a clean baseline)")
    status, body = call(base, "POST", "/api/reset")
    check("reset returns 200", status == 200, f"status={status}")
    check("baseline balance is 3400.00", body.get("balance") == 3400.0, f"got {body.get('balance')}")

    print("\n1. GET /api/health")
    status, body = call(base, "GET", "/api/health")
    check("health 200", status == 200)
    check("MCP reachable", body.get("mcp_reachable") is True)
    check("desk date is 2026-08-31", body.get("desk_date") == "2026-08-31", str(body.get("desk_date")))
    check(
        "CORS allows the Vite origin",
        "http://localhost:5173" in body.get("allowed_origins", []),
    )

    print("\n2. GET /api/tickets")
    status, body = call(base, "GET", "/api/tickets")
    check("tickets 200", status == 200)
    check("three tickets returned", body.get("ticket_count") == 3, str(body.get("ticket_count")))
    ids = sorted(t["ticket_id"] for t in body.get("tickets", []))
    check("ids are 101/102/103", ids == [101, 102, 103], str(ids))
    check(
        "status fields present",
        all("status" in t and "is_resolved" in t for t in body.get("tickets", [])),
    )

    print("\n3. GET /api/cash")
    status, body = call(base, "GET", "/api/cash")
    check("cash 200", status == 200)
    check("balance 3400.00", body.get("balance") == 3400.0, str(body.get("balance")))
    check("ledger empty at baseline", body.get("payments_recorded") == [])

    print("\n4. GET /api/events")
    status, body = call(base, "GET", "/api/events?limit=5")
    check("events 200", status == 200)
    check("events carry seq + event + timestamp",
          all({"seq", "event", "at"} <= set(e) for e in body.get("events", [])))

    print("\n5. POST /api/payments/approve  (guardrails must refuse, cash must not move)")
    before = call(base, "GET", "/api/cash")[1]["balance"]
    status, body = call(base, "POST", "/api/payments/approve",
                        {"kind": "invoice", "ref_id": 501, "amount": 840.0, "approved_by": "Boss"})
    check("agent name rejected with 400", status == 400,
          str(body.get("detail", {}).get("reason", body))[:80])
    status, body = call(base, "POST", "/api/payments/approve",
                        {"kind": "invoice", "ref_id": 501, "amount": 500.0, "approved_by": approver})
    check("wrong amount rejected with 400", status == 400,
          str(body.get("detail", {}).get("reason", body))[:80])
    after = call(base, "GET", "/api/cash")[1]["balance"]
    check("balance unchanged after refusals", before == after, f"{before} -> {after}")

    print("\n6. POST /api/payments/approve  (human approval MUST update cash_accounts)")
    status, body = call(base, "POST", "/api/payments/approve",
                        {"kind": "invoice", "ref_id": 501, "amount": 840.0,
                         "approved_by": approver, "ticket_id": 101})
    check("approval 200", status == 200, str(body)[:80])
    check("payment recorded", body.get("recorded") is True)
    check("balance 3400.00 -> 2560.00",
          (body.get("balance_before"), body.get("balance_after")) == (3400.0, 2560.0),
          f"{body.get('balance_before')} -> {body.get('balance_after')}")
    check("invoice marked paid", body.get("obligation_status") == "paid")

    status, cash = call(base, "GET", "/api/cash")
    check("GET /api/cash reflects the debit", cash.get("balance") == 2560.0, str(cash.get("balance")))
    check("payments row written with the human's name",
          [(p["kind"], p["ref_id"], p["amount"], p["approved_by"]) for p in cash["payments_recorded"]]
          == [("invoice", 501, 840.0, approver)],
          str(cash.get("payments_recorded")))

    status, body = call(base, "POST", "/api/payments/approve",
                        {"kind": "invoice", "ref_id": 501, "amount": 840.0, "approved_by": approver})
    check("double payment refused", status == 400,
          str(body.get("detail", {}).get("reason", body))[:60])

    print("\n7. POST /api/reset  (must restore the database file)")
    dirty = md5(WORKING_DB)
    check("working db differs from seed before reset", dirty != md5(SEED_DB))
    status, body = call(base, "POST", "/api/reset")
    check("reset 200", status == 200)
    check("working db is byte-identical to the seed", md5(WORKING_DB) == md5(SEED_DB))
    check("balance restored to 3400.00", body.get("balance") == 3400.0, str(body.get("balance")))
    check("ledger cleared", body.get("payments_recorded") == 0)
    check("tickets back to open",
          all(t["status"] == "open" for t in body.get("tickets", [])))
    status, cash = call(base, "GET", "/api/cash")
    check("GET /api/cash confirms 3400.00 after reset", cash.get("balance") == 3400.0)

    print("\n8. POST /api/tickets/{id}/run")
    status, body = call(base, "POST", "/api/tickets/999/run", {"wait": False})
    check("unknown ticket returns 404", status == 404, str(body.get("detail"))[:60])
    status, body = call(base, "POST", "/api/tickets/101/run", {"wait": False})
    check("run accepted and returns a run_id", status == 200 and "run_id" in body, str(body)[:80])

    print(f"\n{'=' * 60}\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED: " + "; ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
