"""The five-agent Campus Customs team, built with PydanticAI.

    Boss              - reads the ticket board and routes work
    Inventory         - stock by (sku, size), shortfalls, restock dates
    Accounting        - invoices, cash, margin, payment vouchers
    Facilities        - the lease, rent, the landlord
    Customer Service  - what the customer is actually told

Three things are enforced here in code rather than left to the prompts:

1. **One model, pinned.** Every agent runs `gpt-6-luna` through Portkey using
   PORTKEY_API_KEY. The name is a constant - an env var cannot swap it out.
2. **Facts flow only through MCP.** The agents' entire tool surface is the
   `campus-customs-ops` MCP server read from .mcp.json. There is no second
   database layer in this package; backend/ never opens SQLite itself.
3. **Delegation is budgeted.** Specialists are exposed to each other as tools,
   so any agent can hand off to any other, but every hand-off is counted
   against TokenBudget.max_delegation_turns and logged to the audit trail.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from openai import AsyncOpenAI
from pydantic_ai import Agent, RunContext
from pydantic_ai.mcp import MCPToolset, StdioTransport
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.usage import UsageLimits

from backend.audit import AuditTrail
from backend.models import (
    SPECIALISTS,
    AgentName,
    AgentOutput,
    Delegation,
    DelegationResult,
    TokenBudget,
    ToolInvocation,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
MCP_CONFIG_PATH = PROJECT_ROOT / ".mcp.json"
MCP_SERVER_NAME = "campus-customs-ops"

load_dotenv(PROJECT_ROOT / ".env")

#: The only model this team runs on. Not configurable: the assignment pins it,
#: and a silently swapped model would invalidate every audit trail we produce.
MODEL_NAME = "gpt-6-luna"
DEFAULT_BASE_URL = "https://api.portkey.ai/v1"


# --------------------------------------------------------------------------
# model wiring - Portkey gateway, gpt-6-luna only
# --------------------------------------------------------------------------


def build_model() -> OpenAIChatModel:
    """Configure gpt-6-luna through the Portkey OpenAI-compatible gateway."""
    api_key = os.environ.get("PORTKEY_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "PORTKEY_API_KEY is not set. Copy .env.example to .env and add your key."
        )

    configured = os.environ.get("PORTKEY_MODEL", "").strip()
    if configured and configured != MODEL_NAME:
        # Loud, not silent: a stale PORTKEY_MODEL from an earlier assignment
        # must not quietly redirect this team to a different model.
        raise RuntimeError(
            f"PORTKEY_MODEL is set to {configured!r}, but this team runs {MODEL_NAME!r} only. "
            f"Remove PORTKEY_MODEL from .env or set it to {MODEL_NAME!r}."
        )

    client = AsyncOpenAI(
        api_key=api_key,
        base_url=os.environ.get("PORTKEY_BASE_URL", DEFAULT_BASE_URL),
        default_headers={"x-portkey-api-key": api_key},
    )
    return OpenAIChatModel(MODEL_NAME, provider=OpenAIProvider(openai_client=client))


# --------------------------------------------------------------------------
# run context shared by every agent in one ticket
# --------------------------------------------------------------------------


@dataclass
class DeskDeps:
    """Per-ticket state handed to every agent in the run.

    Carries the audit trail, the shared delegation budget, and the recorded tool
    calls, so a hand-off from Accounting to Inventory is counted and logged
    exactly like one from the Boss.
    """

    ticket_id: int
    audit: AuditTrail
    budget: TokenBudget = field(default_factory=TokenBudget)
    delegations: list[DelegationResult] = field(default_factory=list)
    tool_invocations: list[ToolInvocation] = field(default_factory=list)
    current_agent: AgentName = AgentName.BOSS
    depth: int = 0


# --------------------------------------------------------------------------
# the MCP toolset - the agents' only route to shop facts
# --------------------------------------------------------------------------


def _mcp_server_config() -> dict[str, Any]:
    """Read the server entry from .mcp.json so backend and client agree."""
    if not MCP_CONFIG_PATH.is_file():
        raise RuntimeError(f"No .mcp.json at {MCP_CONFIG_PATH}")
    servers = json.loads(MCP_CONFIG_PATH.read_text(encoding="utf-8")).get("mcpServers", {})
    if MCP_SERVER_NAME not in servers:
        raise RuntimeError(
            f"{MCP_SERVER_NAME!r} is not configured in .mcp.json (found: {list(servers)})"
        )
    return servers[MCP_SERVER_NAME]


async def _log_tool_call(ctx: RunContext[DeskDeps], call_tool, name: str, args: dict[str, Any]):
    """Wrap every MCP tool call so the audit trail sees it.

    PydanticAI routes tool calls through this hook, which is why the trail
    records calls made by any agent without each agent having to report them.
    """
    deps = ctx.deps
    try:
        result = await call_tool(name, args)
    except Exception as exc:  # noqa: BLE001 - recorded, then re-raised
        if deps is not None:
            deps.audit.tool_call(
                agent=deps.current_agent,
                tool_name=name,
                arguments=args,
                result=None,
                ok=False,
                error=f"{type(exc).__name__}: {exc}",
                ticket_id=deps.ticket_id,
            )
        raise

    if deps is not None:
        payload = getattr(result, "structured_content", None)
        if payload is None:
            payload = result if isinstance(result, (dict, list, str, int, float, bool)) else str(result)
        deps.audit.tool_call(
            agent=deps.current_agent,
            tool_name=name,
            arguments=args,
            result=payload,
            ticket_id=deps.ticket_id,
        )
        deps.tool_invocations.append(
            ToolInvocation(
                agent=deps.current_agent,
                tool_name=name,
                arguments=args,
                result=payload,
            )
        )
    return result


def build_toolset() -> MCPToolset:
    """The campus-customs-ops MCP server, launched exactly as .mcp.json declares."""
    cfg = _mcp_server_config()
    return MCPToolset(
        StdioTransport(command=cfg["command"], args=cfg["args"], env=cfg.get("env")),
        process_tool_call=_log_tool_call,
        id=MCP_SERVER_NAME,
    )


# --------------------------------------------------------------------------
# prompts
# --------------------------------------------------------------------------

PROMPT_FILES: dict[AgentName, str] = {
    AgentName.BOSS: "boss.md",
    AgentName.INVENTORY: "inventory.md",
    AgentName.ACCOUNTING: "accounting.md",
    AgentName.FACILITIES: "facilities.md",
    AgentName.CUSTOMER_SERVICE: "customer_service.md",
}


def load_prompt(agent: AgentName) -> str:
    path = PROMPTS_DIR / PROMPT_FILES[agent]
    if not path.is_file():
        raise RuntimeError(f"Prompt file missing for {agent.value}: {path}")
    return path.read_text(encoding="utf-8").strip()


# --------------------------------------------------------------------------
# building the five agents
# --------------------------------------------------------------------------

AGENT_BLURB: dict[AgentName, str] = {
    AgentName.BOSS: "routing, exceptions, and anything needing a decision above the desk",
    AgentName.INVENTORY: "stock counts by SKU and size, shortfalls, restock timing",
    AgentName.ACCOUNTING: "invoices, overdue bills, cash balance, margin, payment vouchers",
    AgentName.FACILITIES: "the lease, rent amounts and due dates, the landlord",
    AgentName.CUSTOMER_SERVICE: "what the customer is told, and what we may not promise",
}


def _agent_usage_limits(budget: TokenBudget) -> UsageLimits:
    """Per-run ceilings. The prompts ask for brevity; these enforce it."""
    return UsageLimits(
        request_limit=budget.max_requests_per_agent,
        tool_calls_limit=budget.max_tool_calls_per_agent,
        total_tokens_limit=budget.max_total_tokens_per_agent,
    )


def _make_delegation_tool(caller: AgentName, target: AgentName):
    """Build the `ask_<target>` tool that `caller` uses to hand work over.

    Every hand-off goes through here, so the budget check, the audit entry, and
    the recursive run are applied identically no matter who delegates to whom.
    """

    async def delegate(ctx: RunContext[DeskDeps], question: str) -> AgentOutput | str:
        deps = ctx.deps
        budget = deps.budget

        if budget.exhausted:
            deps.audit.append(
                "budget_exhausted",
                agent=caller,
                to_agent=target,
                ticket_id=deps.ticket_id,
                message=(
                    f"{caller.value} tried to ask {target.value} but the ticket's "
                    f"{budget.max_delegation_turns}-turn delegation budget is spent."
                ),
                ok=False,
            )
            deps.delegations.append(
                DelegationResult(
                    delegation=Delegation(
                        turn=budget.turns_used,
                        depth=deps.depth + 1,
                        from_agent=caller,
                        to_agent=target,
                        question=question,
                    ),
                    accepted=False,
                    refusal_reason="delegation budget exhausted",
                )
            )
            return (
                f"Delegation refused: the {budget.max_delegation_turns}-turn budget for this "
                "ticket is spent. Conclude with what you already have."
            )

        if deps.depth + 1 > budget.max_delegation_depth:
            deps.audit.delegation(
                caller, target, question, deps.ticket_id, accepted=False,
                reason=f"max depth {budget.max_delegation_depth} exceeded",
            )
            deps.delegations.append(
                DelegationResult(
                    delegation=Delegation(
                        turn=budget.turns_used,
                        depth=deps.depth + 1,
                        from_agent=caller,
                        to_agent=target,
                        question=question,
                    ),
                    accepted=False,
                    refusal_reason="max delegation depth exceeded",
                )
            )
            return "Delegation refused: hand-offs are already nested too deep. Answer directly."

        budget.turns_used += 1
        record = Delegation(
            turn=budget.turns_used,
            depth=deps.depth + 1,
            from_agent=caller,
            to_agent=target,
            question=question,
        )
        deps.audit.delegation(caller, target, question, deps.ticket_id)

        child = DeskDeps(
            ticket_id=deps.ticket_id,
            audit=deps.audit,
            budget=budget,  # shared: one budget for the whole ticket
            delegations=deps.delegations,
            tool_invocations=deps.tool_invocations,
            current_agent=target,
            depth=deps.depth + 1,
        )

        deps.audit.append(
            "agent_started", agent=target, ticket_id=deps.ticket_id, message=question
        )
        result = await get_agent(target).run(
            question, deps=child, usage_limits=_agent_usage_limits(budget)
        )
        output: AgentOutput = result.output
        deps.audit.append(
            "agent_finished",
            agent=target,
            ticket_id=deps.ticket_id,
            message=output.summary,
            ok=True,
        )
        for voucher in output.prepared_vouchers:
            deps.audit.append(
                "voucher_prepared",
                agent=target,
                ticket_id=deps.ticket_id,
                message=(
                    f"{voucher.voucher_id}: ${voucher.amount:.2f} to {voucher.payee} "
                    "- awaiting human approval"
                ),
            )

        deps.delegations.append(DelegationResult(delegation=record, accepted=True, output=output))
        return output

    delegate.__name__ = f"ask_{target.value}"
    delegate.__doc__ = (
        f"Hand this question to the {target.value.replace('_', ' ')} agent, who owns "
        f"{AGENT_BLURB[target]}. Include the ticket's concrete facts (ticket id, SKU, size, "
        f"quantity, invoice or lease id) so they do not have to guess what you mean. "
        f"Returns their structured findings. Costs one of the ticket's delegation turns, "
        f"so ask only when their answer would change yours."
    )
    return delegate


@lru_cache(maxsize=None)
def get_agent(name: AgentName) -> Agent[DeskDeps, AgentOutput]:
    """Build (and cache) one agent, wired to MCP and to its delegation peers."""
    peers = [a for a in (AgentName.BOSS, *SPECIALISTS) if a != name]
    if name == AgentName.BOSS:
        peers = list(SPECIALISTS)  # the Boss does not delegate to itself

    return Agent(
        build_model(),
        output_type=AgentOutput,
        deps_type=DeskDeps,
        instructions=load_prompt(name),
        toolsets=[build_toolset()],
        tools=[_make_delegation_tool(name, peer) for peer in peers],
        name=name.value,
        retries=2,
    )


def reset_agent_cache() -> None:
    """Drop cached agents. Used by tests that swap in a stub model."""
    get_agent.cache_clear()
