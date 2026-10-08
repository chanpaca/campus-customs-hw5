/** Shapes returned by the FastAPI backend. Mirrors backend/models.py. */

export type TicketStatus =
  | 'open'
  | 'in_progress'
  | 'awaiting_approval'
  | 'resolved'
  | 'blocked'

export interface Ticket {
  ticket_id: number
  type: string
  requester: string
  subject: string
  sku: string | null
  size: string | null
  qty: number | null
  lease_id: number | null
  invoice_id: number | null
  status: TicketStatus
  notes: string | null
  created_at: string
  is_resolved: boolean
  needs_human: boolean
}

export interface TicketsResponse {
  as_of: string
  ticket_count: number
  open_count: number
  tickets: Ticket[]
}

export interface PaymentRow {
  payment_id: number
  kind: string
  ref_id: number | null
  amount: number
  account: string
  paid_at: string
  approved_by: string
}

export interface CashResponse {
  as_of: string
  found: boolean
  account: string
  balance: number
  balance_as_of: string
  payments_recorded: PaymentRow[]
  total_disbursed: number
}

export type AgentName =
  | 'boss'
  | 'inventory'
  | 'accounting'
  | 'facilities'
  | 'customer_service'

export type EventKind =
  | 'run_started'
  | 'agent_started'
  | 'delegation'
  | 'delegation_refused'
  | 'tool_call'
  | 'agent_finished'
  | 'voucher_prepared'
  | 'human_approval_required'
  | 'budget_exhausted'
  | 'error'
  | 'run_finished'

export interface AgentEvent {
  run_id: string
  /** Monotonic position in the append-only trail. The polling cursor. */
  index: number
  /** Position within its own run; restarts at 1 each run. Not a cursor. */
  seq: number
  at: string
  ticket_id: number | null
  event: EventKind
  agent: AgentName | null
  to_agent: AgentName | null
  message: string | null
  tool_name: string | null
  tool_args: Record<string, unknown> | null
  tool_result: Record<string, unknown> | null
  ok: boolean
}

export interface EventsResponse {
  count: number
  last_seq: number
  total_events: number
  events: AgentEvent[]
}

/** A voucher an agent prepared, reconstructed from the draft tool's result. */
export interface Voucher {
  voucher_id: string
  kind: 'invoice' | 'rent'
  ref_id: number
  payee: string | null
  amount: number
  memo: string | null
  due_date: string | null
  days_past_due: number | null
  balance_before: number | null
  balance_after_if_approved: number | null
  ticket_id: number | null
  preparedBy: AgentName | null
  seq: number
}

export interface ApprovalResult {
  recorded: boolean
  payment_id: number | null
  amount: number
  approved_by: string
  balance_before: number
  balance_after: number
  obligation_status: string | null
}

export interface RunState {
  run_id: string
  ticket_id: number
  status: 'running' | 'finished' | 'failed'
  started_at: string
  finished_at: string | null
  error: string | null
  result: RunResult | null
}

export interface CitedFact {
  fact: string
  source_tool: string
}

export interface AgentOutput {
  agent: AgentName
  summary: string
  facts_used: CitedFact[]
  recommended_actions: string[]
  prepared_vouchers: { voucher_id: string; amount: number; payee: string | null }[]
  blocking_issues: string[]
  needs_human: boolean
  customer_message: string | null
  proposed_ticket_status: TicketStatus
}

export interface RunResult {
  run_id: string
  ticket_id: number
  desk_date: string | null
  model: string
  boss_output: AgentOutput | null
  final_state: {
    status: TicketStatus
    needs_human: boolean
    blocking_issues: string[]
  } | null
  delegations: {
    delegation: { from_agent: AgentName; to_agent: AgentName; turn: number; question: string }
    accepted: boolean
    refusal_reason: string | null
  }[]
  tool_invocations: { agent: AgentName; tool_name: string }[]
  budget: { max_delegation_turns: number; turns_used: number }
  error: string | null
}
