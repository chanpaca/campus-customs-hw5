"""Structured data models for the Campus Customs agent team.

Four families of model live here:

    AgentName / AgentOutput / PreparedVoucher   - what an agent says when it finishes
    Delegation / DelegationResult               - one agent handing work to another
    ToolInvocation                              - one MCP tool call and what came back
    TicketState / TicketRun / AuditEvent        - the state of a ticket and the trail

Everything an agent produces is typed. The agents return `AgentOutput` rather than
free text, so the orchestrator can check whether a voucher was prepared, whether a
human is still needed, and which facts were cited - instead of parsing prose.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


def _utcnow() -> str:
    """Wall-clock stamp for the audit trail only.

    Note the distinction: audit entries record when *we ran*, which is real time.
    Every business date (overdue, due-in, paid_at) comes from desk.date_today
    instead - see the MCP tools. These two clocks must never be confused.
    """
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# who is on the team
# --------------------------------------------------------------------------


class AgentName(str, Enum):
    """The five agents. Used as the routing key and as the audit trail's actor."""

    BOSS = "boss"
    INVENTORY = "inventory"
    ACCOUNTING = "accounting"
    FACILITIES = "facilities"
    CUSTOMER_SERVICE = "customer_service"


SPECIALISTS: tuple[AgentName, ...] = (
    AgentName.INVENTORY,
    AgentName.ACCOUNTING,
    AgentName.FACILITIES,
    AgentName.CUSTOMER_SERVICE,
)


# --------------------------------------------------------------------------
# what an agent produces
# --------------------------------------------------------------------------


class CitedFact(BaseModel):
    """One fact an agent used, with the tool that produced it.

    This is how 'no invented facts' becomes checkable rather than aspirational:
    an agent that cannot name the tool behind a number should not state it.
    """

    fact: str = Field(description="The fact as used, e.g. 'invoice 501 is $840.00 and 3 days overdue'.")
    source_tool: str = Field(description="MCP tool that returned it, e.g. 'get_vendor_invoices'.")


class PreparedVoucher(BaseModel):
    """A disbursement an agent prepared for a human to approve. Never a payment."""

    voucher_id: str = Field(description="Voucher handle, e.g. 'VCH-INVOICE-501'.")
    kind: Literal["invoice", "rent"] = Field(description="What is being settled.")
    ref_id: int = Field(description="invoices.id or leases.id.")
    payee: str | None = Field(default=None, description="Who would be paid.")
    amount: float = Field(description="Amount read from the obligation, not chosen by the agent.")
    requires_human_approval: Literal[True] = Field(
        default=True,
        description="Always True. An agent that sets this False has violated the control.",
    )
    rationale: str = Field(description="Why this payment should be approved.")


class AgentOutput(BaseModel):
    """The structured result every agent returns instead of free text."""

    agent: AgentName = Field(description="Which agent produced this.")
    summary: str = Field(description="What this agent concluded, in plain language.")
    facts_used: list[CitedFact] = Field(
        default_factory=list, description="Facts cited, each tied to the tool that produced it."
    )
    recommended_actions: list[str] = Field(
        default_factory=list, description="Concrete next steps, in order."
    )
    prepared_vouchers: list[PreparedVoucher] = Field(
        default_factory=list, description="Vouchers drafted and awaiting a human."
    )
    blocking_issues: list[str] = Field(
        default_factory=list,
        description="What prevents resolution, e.g. 'invoice 501 unpaid: no ship'.",
    )
    needs_human: bool = Field(
        default=False, description="True when a human must act before this can close."
    )
    customer_message: str | None = Field(
        default=None, description="Message to send the requester. Customer Service only."
    )
    proposed_ticket_status: Literal[
        "open", "in_progress", "awaiting_approval", "resolved", "blocked"
    ] = Field(default="in_progress", description="Status this agent believes the ticket is in.")


# --------------------------------------------------------------------------
# agents talking to each other
# --------------------------------------------------------------------------


class Delegation(BaseModel):
    """One agent handing a question to another."""

    turn: int = Field(description="Delegation turn number within this ticket, starting at 1.")
    depth: int = Field(description="How many hand-offs deep this is. Boss to specialist is 1.")
    from_agent: AgentName = Field(description="Who delegated.")
    to_agent: AgentName = Field(description="Who was asked.")
    question: str = Field(description="What was asked, including the ticket's concrete facts.")
    at: str = Field(default_factory=_utcnow, description="Wall-clock time of the hand-off.")


class DelegationResult(BaseModel):
    """A delegation and what came back, including refusals."""

    delegation: Delegation
    accepted: bool = Field(
        default=True, description="False when refused - budget exhausted, or depth exceeded."
    )
    refusal_reason: str | None = Field(default=None, description="Why it was refused.")
    output: AgentOutput | None = Field(default=None, description="What the called agent returned.")


# --------------------------------------------------------------------------
# tool calls
# --------------------------------------------------------------------------


class ToolInvocation(BaseModel):
    """One MCP tool call: who called it, with what, and what came back."""

    agent: AgentName = Field(description="Agent that made the call.")
    tool_name: str = Field(description="MCP tool invoked, e.g. 'get_cash_balance'.")
    arguments: dict[str, Any] = Field(default_factory=dict, description="Arguments sent.")
    result: Any = Field(default=None, description="Verbatim result returned by the tool.")
    ok: bool = Field(default=True, description="False when the tool raised or refused.")
    error: str | None = Field(default=None, description="Error text when ok is False.")
    at: str = Field(default_factory=_utcnow, description="Wall-clock time of the call.")


# --------------------------------------------------------------------------
# ticket state and the run
# --------------------------------------------------------------------------


class TicketState(BaseModel):
    """Where a ticket stands, as the team understands it."""

    ticket_id: int = Field(description="Ticket id, e.g. 101.")
    type: str | None = Field(default=None, description="'customer_order', 'rent_notice', ...")
    requester: str | None = Field(default=None, description="Who opened it.")
    subject: str | None = Field(default=None, description="One-line subject.")
    status: Literal["open", "in_progress", "awaiting_approval", "resolved", "blocked"] = Field(
        default="open", description="Current status."
    )
    owner: AgentName | None = Field(default=None, description="Agent currently responsible.")
    blocking_issues: list[str] = Field(default_factory=list, description="What is in the way.")
    vouchers: list[PreparedVoucher] = Field(
        default_factory=list, description="Vouchers prepared, awaiting a human."
    )
    needs_human: bool = Field(default=False, description="True when a human must act.")


class TokenBudget(BaseModel):
    """Hard limits for one ticket. Enforced by the orchestrator, not by the prompts.

    A prompt asking an agent to be brief is a request. These are the ceiling.
    """

    max_delegation_turns: int = Field(
        default=10, description="Total hand-offs allowed per ticket, across all agents."
    )
    max_delegation_depth: int = Field(
        default=3, description="How deep hand-offs may nest, to stop A->B->A->B loops."
    )
    max_requests_per_agent: int = Field(
        default=12, description="Model requests one agent may make in a single run."
    )
    max_tool_calls_per_agent: int = Field(
        default=15, description="MCP tool calls one agent may make in a single run."
    )
    max_total_tokens_per_agent: int = Field(
        default=60_000, description="Token ceiling for one agent run."
    )

    turns_used: int = Field(default=0, description="Delegation turns consumed so far.")

    @property
    def turns_remaining(self) -> int:
        return max(0, self.max_delegation_turns - self.turns_used)

    @property
    def exhausted(self) -> bool:
        return self.turns_used >= self.max_delegation_turns


class AuditEvent(BaseModel):
    """One append-only line in output/audit_trail.json.

    Deliberately flat: an event names the agent, what happened, the tool involved,
    and the result. A reviewer can read the file top to bottom and reconstruct the
    run without replaying it.
    """

    run_id: str = Field(description="Groups every event from one orchestrator run.")
    seq: int = Field(description="Position within the run, starting at 1.")
    mode: Literal["agent", "scripted"] = Field(
        default="agent",
        description=(
            "'agent' means an LLM chose this step. 'scripted' means a deterministic "
            "runner executed the pre-planned sequence with no model in the loop - the "
            "tool calls and their results are still real, but the decisions were not "
            "made by a model. Never label a scripted step as 'agent'."
        ),
    )
    at: str = Field(default_factory=_utcnow, description="Wall-clock time (not the desk date).")
    ticket_id: int | None = Field(default=None, description="Ticket this event belongs to.")
    event: Literal[
        "run_started",
        "agent_started",
        "delegation",
        "delegation_refused",
        "tool_call",
        "agent_finished",
        "voucher_prepared",
        "human_approval_required",
        "budget_exhausted",
        "error",
        "run_finished",
    ] = Field(description="What kind of step this was.")
    agent: AgentName | None = Field(default=None, description="Agent acting.")
    to_agent: AgentName | None = Field(default=None, description="Target, for delegations.")
    message: str | None = Field(default=None, description="Human-readable description.")
    tool_name: str | None = Field(default=None, description="MCP tool, for tool_call events.")
    tool_args: dict[str, Any] | None = Field(default=None, description="Arguments sent.")
    tool_result: Any = Field(default=None, description="Verbatim tool result.")
    ok: bool = Field(default=True, description="False for errors and refusals.")


class TicketRun(BaseModel):
    """The full result of running one ticket through the team."""

    run_id: str = Field(description="Run identifier, shared by every audit event.")
    ticket_id: int = Field(description="Ticket that was run.")
    started_at: str = Field(default_factory=_utcnow, description="Wall-clock start.")
    finished_at: str | None = Field(default=None, description="Wall-clock finish.")
    desk_date: str | None = Field(default=None, description="desk.date_today for this run.")
    model: str = Field(description="Model used, e.g. 'gpt-6-luna'.")
    final_state: TicketState | None = Field(default=None, description="Where the ticket ended.")
    boss_output: AgentOutput | None = Field(default=None, description="The Boss's conclusion.")
    delegations: list[DelegationResult] = Field(
        default_factory=list, description="Every hand-off attempted, including refusals."
    )
    tool_invocations: list[ToolInvocation] = Field(
        default_factory=list, description="Every MCP tool call made during the run."
    )
    budget: TokenBudget = Field(default_factory=TokenBudget, description="Budget and what was used.")
    error: str | None = Field(default=None, description="Set when the run failed.")
