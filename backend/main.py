"""FastAPI backend for the Campus Customs agent dashboard.

Routes the React frontend calls:

    GET  /api/tickets              the board, with live status
    POST /api/tickets/{id}/run     turn the agent team loose on one ticket
    GET  /api/events               recent agent activity, for the live feed
    POST /api/payments/approve     a HUMAN approves a voucher - the only cash path
    GET  /api/cash                 current checking balance
    POST /api/reset                restore the working database from the seed

Two rules shape this module:

1. **Shop facts come from MCP, not from SQLite.** No route here opens the
   database. Tickets and cash are read through the `campus-customs-ops` server,
   the same tools the agents use, so the dashboard and the agents can never
   disagree about what is true. The one exception is /api/reset, which copies a
   file rather than querying one.

2. **Agents prepare, humans approve.** /api/payments/approve is deliberately not
   reachable from inside an agent run. It takes the name of the person who
   clicked the button and hands it to `record_approved_payment`, which rejects
   agent names outright.

Run:  python -m uvicorn backend.main:app --port 8001 --reload
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Work under both launch styles:
#   uvicorn backend.main:app          (from the project root)
#   uvicorn main:app --app-dir backend   (with backend/ on sys.path)
# The second puts backend/ on sys.path but not the project root, so the
# `backend.*` imports below would fail without this.
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

load_dotenv(PROJECT_ROOT / ".env")

from backend.audit import AuditTrail, read_recent, read_trail  # noqa: E402
from backend.models import TokenBudget  # noqa: E402
from backend.orchestrator import _mcp_call, approve_voucher, run_ticket  # noqa: E402

SEED_DB = PROJECT_ROOT / "data" / "campus_customs.db"
WORKING_DB = PROJECT_ROOT / "data" / "campus_customs_new.db"

#: The Vite dev server. 5174 is kept because another project in this repo
#: already holds 5173, and a blocked port should not break the dashboard.
ALLOWED_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:5174",
    "http://127.0.0.1:5174",
    # 5173 and 5174 are both held by other dev servers on this machine, so the
    # dashboard was verified from 5180; kept so that run is reproducible.
    "http://localhost:5180",
    "http://127.0.0.1:5180",
]

app = FastAPI(
    title="Campus Customs Agent Desk",
    version="1.0.0",
    description="Backend for the multi-agent operations dashboard (MGT 409, HW5).",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

#: In-flight and completed runs, keyed by run_id. The frontend starts a run and
#: then follows /api/events; this is just enough state to report run status.
RUNS: dict[str, dict[str, Any]] = {}


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# request / response models
# --------------------------------------------------------------------------


class RunRequest(BaseModel):
    """Options for a ticket run."""

    wait: bool = Field(
        default=False,
        description="True runs synchronously and returns the result; False returns immediately "
        "and the frontend follows /api/events.",
    )
    max_delegation_turns: int = Field(
        default=10, ge=1, le=25, description="Delegation budget for this ticket."
    )


class ApprovalRequest(BaseModel):
    """A human approving a prepared voucher. This is what moves money."""

    kind: Literal["invoice", "rent"] = Field(description="What is being settled.")
    ref_id: int = Field(description="invoices.id or leases.id.")
    amount: float = Field(gt=0, description="Amount approved; must match the obligation exactly.")
    approved_by: str = Field(
        min_length=2,
        description="Name of the human approving. Agent names are rejected downstream.",
    )
    ticket_id: int | None = Field(default=None, description="Ticket this approval belongs to.")


# --------------------------------------------------------------------------
# 1. GET /api/tickets
# --------------------------------------------------------------------------


@app.get("/api/tickets", summary="All tickets with their current status")
async def get_tickets() -> dict[str, Any]:
    """Return every ticket and its status, read through MCP."""
    try:
        board = await _mcp_call("get_open_tickets", {"status": "all"})
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"MCP server unreachable: {type(exc).__name__}: {exc}") from exc

    tickets = (board or {}).get("tickets", [])
    for ticket in tickets:
        ticket["is_resolved"] = ticket.get("status") == "resolved"
        ticket["needs_human"] = ticket.get("status") == "awaiting_approval"
    return {
        "as_of": (board or {}).get("as_of"),
        "ticket_count": len(tickets),
        "open_count": sum(1 for t in tickets if t.get("status") != "resolved"),
        "tickets": tickets,
    }


# --------------------------------------------------------------------------
# 2. POST /api/tickets/{id}/run
# --------------------------------------------------------------------------


async def _run_and_record(run_id: str, ticket_id: int, budget: TokenBudget) -> None:
    """Execute one ticket and park the outcome in RUNS for status polling."""
    try:
        result = await run_ticket(ticket_id, budget=budget, audit=AuditTrail(run_id))
        RUNS[run_id].update(
            status="failed" if result.error else "finished",
            finished_at=_utcnow(),
            error=result.error,
            result=result.model_dump(mode="json"),
        )
    except Exception as exc:  # noqa: BLE001
        RUNS[run_id].update(
            status="failed", finished_at=_utcnow(), error=f"{type(exc).__name__}: {exc}"
        )


@app.post("/api/tickets/{ticket_id}/run", summary="Run the agent team on one ticket")
async def run_ticket_route(ticket_id: int, options: RunRequest | None = None) -> dict[str, Any]:
    """Turn the five-agent team loose on a ticket.

    By default the run starts in the background and this returns immediately with
    a run_id - the dashboard then follows /api/events for the live feed. Pass
    `wait: true` to block until the team finishes.
    """
    options = options or RunRequest()
    board = await get_tickets()
    if not any(t.get("ticket_id") == ticket_id for t in board["tickets"]):
        raise HTTPException(404, f"Ticket {ticket_id} is not on the board.")

    run_id = f"run-{ticket_id}-{uuid.uuid4().hex[:8]}"
    budget = TokenBudget(max_delegation_turns=options.max_delegation_turns)
    RUNS[run_id] = {
        "run_id": run_id,
        "ticket_id": ticket_id,
        "status": "running",
        "started_at": _utcnow(),
        "finished_at": None,
        "error": None,
        "result": None,
    }

    if options.wait:
        await _run_and_record(run_id, ticket_id, budget)
        return RUNS[run_id]

    asyncio.create_task(_run_and_record(run_id, ticket_id, budget))
    return {
        "run_id": run_id,
        "ticket_id": ticket_id,
        "status": "running",
        "started_at": RUNS[run_id]["started_at"],
        "follow": "/api/events",
    }


@app.get("/api/runs/{run_id}", summary="Status of one ticket run")
async def get_run(run_id: str) -> dict[str, Any]:
    """Poll a background run started by POST /api/tickets/{id}/run."""
    if run_id not in RUNS:
        raise HTTPException(404, f"No run {run_id}.")
    return RUNS[run_id]


# --------------------------------------------------------------------------
# 3. GET /api/events
# --------------------------------------------------------------------------


@app.get("/api/events", summary="Recent agent activity for the live feed")
async def get_events(
    limit: int = 50, since_seq: int | None = None, ticket_id: int | None = None, run_id: str | None = None
) -> dict[str, Any]:
    """Recent audit events: agent messages, delegations, tool calls, timestamps.

    Each event is tagged with `index`, its 1-based position in the append-only
    trail. `seq` restarts at 1 for every run, so it cannot be used as a polling
    cursor - `index` is the monotonic one, and `since_seq` filters on it.
    """
    trail = read_trail()
    events = [{**event, "index": i} for i, event in enumerate(trail, start=1)]

    if since_seq is not None:
        events = [e for e in events if e["index"] > since_seq]
    if ticket_id is not None:
        events = [e for e in events if e.get("ticket_id") == ticket_id]
    if run_id is not None:
        events = [e for e in events if e.get("run_id") == run_id]

    highest = len(trail)
    events = events[-limit:]
    return {
        "count": len(events),
        "last_seq": events[-1]["index"] if events else (since_seq if since_seq is not None else highest),
        "total_events": highest,
        "events": events,
    }


# --------------------------------------------------------------------------
# 4. POST /api/payments/approve  -- the only path that moves money
# --------------------------------------------------------------------------


@app.post("/api/payments/approve", summary="Human approves a voucher; records the payment")
async def approve_payment(request: ApprovalRequest) -> dict[str, Any]:
    """Record a payment a human has approved.

    This is the hinge of the whole design. Agents can only draft vouchers; this
    route is what writes the `payments` row and debits `cash_accounts`. It is
    never called from inside an agent run, and the MCP tool behind it rejects
    agent names in `approved_by`, so an agent cannot launder its own approval
    through the dashboard.
    """
    result = await approve_voucher(
        kind=request.kind,
        ref_id=request.ref_id,
        amount=request.amount,
        approved_by=request.approved_by.strip(),
        ticket_id=request.ticket_id,
    )
    if not (result or {}).get("recorded"):
        # A refused payment is a 400, not a 500: the guardrail did its job.
        raise HTTPException(
            400,
            detail={
                "recorded": False,
                "reason": (result or {}).get("rejected_reason", "payment refused"),
                "result": result,
            },
        )
    return result


# --------------------------------------------------------------------------
# 5. GET /api/cash
# --------------------------------------------------------------------------


@app.get("/api/cash", summary="Current checking balance")
async def get_cash(account: str = "checking") -> dict[str, Any]:
    """Current balance plus everything already disbursed, read through MCP."""
    try:
        cash = await _mcp_call("get_cash_balance", {"account": account})
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"MCP server unreachable: {type(exc).__name__}: {exc}") from exc
    if not (cash or {}).get("found"):
        raise HTTPException(404, (cash or {}).get("not_found_reason", f"No account {account!r}."))
    return cash


# --------------------------------------------------------------------------
# 6. POST /api/reset
# --------------------------------------------------------------------------


@app.post("/api/reset", summary="Restore the working database from the pristine seed")
async def reset_database() -> dict[str, Any]:
    """Copy data/campus_customs.db over data/campus_customs_new.db.

    Returns the desk to its $3,400.00 starting state for a fresh run. This is a
    file copy, not a database query - the seed is never opened, only duplicated,
    so there is still no second data layer behind the agents' backs.

    The audit trail is deliberately NOT cleared: it is append-only, and a reset
    is itself an event worth recording.
    """
    if not SEED_DB.is_file():
        raise HTTPException(500, f"Seed database missing at {SEED_DB}")

    shutil.copy2(SEED_DB, WORKING_DB)

    trail = AuditTrail(f"reset-{uuid.uuid4().hex[:8]}")
    trail.append(
        "run_started", message=f"Database reset from {SEED_DB.name} to {WORKING_DB.name}."
    )

    try:
        cash = await _mcp_call("get_cash_balance", {"account": "checking"})
        board = await _mcp_call("get_open_tickets", {"status": "all"})
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"reset copied, but MCP read-back failed: {exc}") from exc

    trail.append(
        "run_finished",
        message=f"Reset complete: balance {(cash or {}).get('balance')}, "
        f"{(board or {}).get('ticket_count')} tickets.",
    )
    RUNS.clear()

    return {
        "reset": True,
        "seed": str(SEED_DB.relative_to(PROJECT_ROOT)),
        "working": str(WORKING_DB.relative_to(PROJECT_ROOT)),
        "balance": (cash or {}).get("balance"),
        "payments_recorded": len((cash or {}).get("payments_recorded", [])),
        "tickets": [
            {"ticket_id": t.get("ticket_id"), "status": t.get("status")}
            for t in (board or {}).get("tickets", [])
        ],
        "note": "Audit trail preserved: it is append-only by design.",
    }


# --------------------------------------------------------------------------
# health
# --------------------------------------------------------------------------


@app.get("/api/health", summary="Service health and MCP reachability")
async def health() -> dict[str, Any]:
    """Confirm the API is up and the MCP server answers."""
    try:
        board = await _mcp_call("get_open_tickets", {"status": "all"})
        mcp_ok, desk_date = True, (board or {}).get("as_of")
    except Exception as exc:  # noqa: BLE001
        mcp_ok, desk_date = False, None
        mcp_error = f"{type(exc).__name__}: {exc}"
    else:
        mcp_error = None

    return {
        "ok": mcp_ok,
        "mcp_server": "campus-customs-ops",
        "mcp_reachable": mcp_ok,
        "mcp_error": mcp_error,
        "desk_date": desk_date,
        "allowed_origins": ALLOWED_ORIGINS,
        "audit_events": len(read_trail()),
    }
