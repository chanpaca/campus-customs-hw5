"""The agent execution loop for one Campus Customs ticket.

Flow:

    run_ticket(101)
        -> append run_started to the audit trail
        -> read the ticket from MCP (never from SQLite directly)
        -> run the Boss, who delegates to specialists as needed
        -> every delegation and every MCP tool call is appended to the trail
        -> append run_finished, return a typed TicketRun

Nothing here approves a payment. The agents may prepare vouchers; turning one
into money is `approve_voucher()`, which requires a named human and is called
from the backend route a person clicks - never from inside an agent run.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from backend.agents import (
    MODEL_NAME,
    DeskDeps,
    build_toolset,
    get_agent,
    _agent_usage_limits,
)
from backend.audit import AuditTrail
from backend.models import (
    AgentName,
    AgentOutput,
    TicketRun,
    TicketState,
    TokenBudget,
)


def _new_run_id(ticket_id: int) -> str:
    return f"run-{ticket_id}-{uuid.uuid4().hex[:8]}"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def _mcp_call(tool_name: str, args: dict[str, Any]) -> Any:
    """Call one MCP tool outside an agent run (for orchestrator bookkeeping).

    Still MCP - the orchestrator has no database access of its own either.
    """
    toolset = build_toolset()
    async with toolset:
        result = await toolset.direct_call_tool(tool_name, args)
    return getattr(result, "structured_content", result)


async def run_ticket(
    ticket_id: int,
    budget: TokenBudget | None = None,
    audit: AuditTrail | None = None,
) -> TicketRun:
    """Run one ticket through the team and return the typed result."""
    run_id = _new_run_id(ticket_id)
    trail = audit or AuditTrail(run_id)
    budget = budget or TokenBudget()

    run = TicketRun(run_id=run_id, ticket_id=ticket_id, model=MODEL_NAME, budget=budget)

    trail.append(
        "run_started",
        ticket_id=ticket_id,
        agent=AgentName.BOSS,
        message=f"Ticket {ticket_id} handed to the desk (model {MODEL_NAME}).",
    )

    # Read the ticket through MCP so the run starts from recorded fact.
    try:
        board = await _mcp_call("get_open_tickets", {"status": "all"})
        trail.tool_call(
            agent=AgentName.BOSS,
            tool_name="get_open_tickets",
            arguments={"status": "all"},
            result=board,
            ticket_id=ticket_id,
        )
    except Exception as exc:  # noqa: BLE001
        run.error = f"could not read the ticket board: {type(exc).__name__}: {exc}"
        trail.append("error", ticket_id=ticket_id, message=run.error, ok=False)
        trail.append("run_finished", ticket_id=ticket_id, message=run.error, ok=False)
        run.finished_at = _utcnow()
        return run

    tickets = (board or {}).get("tickets", [])
    record = next((t for t in tickets if t.get("ticket_id") == ticket_id), None)
    run.desk_date = (board or {}).get("as_of")

    if record is None:
        run.error = f"Ticket {ticket_id} is not on the board."
        trail.append("error", ticket_id=ticket_id, message=run.error, ok=False)
        trail.append("run_finished", ticket_id=ticket_id, message=run.error, ok=False)
        run.finished_at = _utcnow()
        return run

    state = TicketState(
        ticket_id=ticket_id,
        type=record.get("type"),
        requester=record.get("requester"),
        subject=record.get("subject"),
        status="in_progress",
        owner=AgentName.BOSS,
    )

    deps = DeskDeps(ticket_id=ticket_id, audit=trail, budget=budget, current_agent=AgentName.BOSS)
    task = _boss_brief(record, run.desk_date)

    trail.append("agent_started", agent=AgentName.BOSS, ticket_id=ticket_id, message=task)

    try:
        result = await get_agent(AgentName.BOSS).run(
            task, deps=deps, usage_limits=_agent_usage_limits(budget)
        )
        output: AgentOutput = result.output
    except Exception as exc:  # noqa: BLE001
        run.error = f"{type(exc).__name__}: {exc}"
        trail.append("error", agent=AgentName.BOSS, ticket_id=ticket_id, message=run.error, ok=False)
        trail.append("run_finished", ticket_id=ticket_id, message=run.error, ok=False)
        run.finished_at = _utcnow()
        run.delegations = deps.delegations
        run.tool_invocations = deps.tool_invocations
        return run

    trail.append(
        "agent_finished", agent=AgentName.BOSS, ticket_id=ticket_id, message=output.summary
    )

    for voucher in output.prepared_vouchers:
        trail.append(
            "voucher_prepared",
            agent=AgentName.BOSS,
            ticket_id=ticket_id,
            message=(
                f"{voucher.voucher_id}: ${voucher.amount:.2f} to {voucher.payee} "
                "- awaiting human approval"
            ),
        )

    state.status = output.proposed_ticket_status
    state.blocking_issues = output.blocking_issues
    state.vouchers = output.prepared_vouchers
    state.needs_human = output.needs_human or bool(output.prepared_vouchers)

    if state.needs_human:
        trail.append(
            "human_approval_required",
            ticket_id=ticket_id,
            agent=AgentName.BOSS,
            message=(
                f"Ticket {ticket_id} cannot close without a human: "
                f"{len(output.prepared_vouchers)} voucher(s) awaiting approval."
            ),
        )

    run.boss_output = output
    run.final_state = state
    run.delegations = deps.delegations
    run.tool_invocations = deps.tool_invocations
    run.budget = budget
    run.finished_at = _utcnow()

    trail.append(
        "run_finished",
        ticket_id=ticket_id,
        agent=AgentName.BOSS,
        message=(
            f"Ticket {ticket_id} -> {state.status}; "
            f"{budget.turns_used}/{budget.max_delegation_turns} delegation turns used; "
            f"{len(deps.tool_invocations)} MCP tool calls."
        ),
    )
    return run


def _boss_brief(record: dict[str, Any], desk_date: str | None) -> str:
    """The task handed to the Boss: the ticket's recorded facts, nothing added."""
    lines = [
        f"Ticket {record.get('ticket_id')} is on your desk. Work it to a decision.",
        "",
        "Recorded facts (from the tickets table):",
        f"  type:       {record.get('type')}",
        f"  requester:  {record.get('requester')}",
        f"  subject:    {record.get('subject')}",
        f"  status:     {record.get('status')}",
    ]
    for key, label in (
        ("sku", "sku"),
        ("size", "size"),
        ("qty", "qty"),
        ("invoice_id", "invoice_id"),
        ("lease_id", "lease_id"),
    ):
        if record.get(key) is not None:
            lines.append(f"  {label + ':':11} {record.get(key)}")
    if record.get("notes"):
        lines.append(f"  notes:      {record.get('notes')}  (a claim, verify it)")
    if desk_date:
        lines.append("")
        lines.append(f"The desk's reference date is {desk_date}. Judge every date against it.")
    lines.append("")
    lines.append(
        "Route this to the specialist who owns it, gather what they find, and say plainly "
        "what you recommend and what still needs a human."
    )
    return "\n".join(lines)


# --------------------------------------------------------------------------
# human approval - deliberately outside the agent loop
# --------------------------------------------------------------------------


async def approve_voucher(
    kind: str,
    ref_id: int,
    amount: float,
    approved_by: str,
    ticket_id: int | None = None,
    audit: AuditTrail | None = None,
) -> Any:
    """Record a payment a HUMAN approved. Never called from inside an agent run.

    The backend route behind the dashboard's approve button calls this with the
    name of the person who clicked it. The MCP tool rejects agent names, so this
    path cannot be used to launder an agent's own approval.
    """
    trail = audit or AuditTrail(f"approval-{uuid.uuid4().hex[:8]}")
    trail.append(
        "human_approval_required",
        ticket_id=ticket_id,
        message=f"{approved_by} approving {kind} {ref_id} for ${amount:.2f}",
    )
    result = await _mcp_call(
        "record_approved_payment",
        {"kind": kind, "ref_id": ref_id, "amount": amount, "approved_by": approved_by},
    )
    trail.tool_call(
        agent=None,
        tool_name="record_approved_payment",
        arguments={
            "kind": kind,
            "ref_id": ref_id,
            "amount": amount,
            "approved_by": approved_by,
        },
        result=result,
        ok=bool((result or {}).get("recorded")),
        ticket_id=ticket_id,
    )
    return result
