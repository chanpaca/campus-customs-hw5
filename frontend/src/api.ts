/**
 * Typed client for the Campus Customs backend.
 *
 * Requests go to same-origin `/api/*` and Vite proxies them to the FastAPI
 * server (http://localhost:8000 by default, see vite.config.ts). Set
 * VITE_API_BASE to call an absolute URL instead.
 */

import type {
  ApprovalResult,
  CashResponse,
  EventsResponse,
  RunState,
  TicketsResponse,
} from './types'

const BASE = (import.meta.env.VITE_API_BASE ?? '').replace(/\/$/, '')

export class ApiError extends Error {
  status: number
  /** The backend's structured refusal, e.g. a guardrail explaining itself. */
  detail: unknown

  constructor(message: string, status: number, detail: unknown) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  })

  const raw = await response.text()
  let body: unknown = null
  try {
    body = raw ? JSON.parse(raw) : null
  } catch {
    body = raw
  }

  if (!response.ok) {
    const detail = (body as { detail?: unknown })?.detail ?? body
    const reason =
      (detail as { reason?: string })?.reason ??
      (typeof detail === 'string' ? detail : null) ??
      `Request failed (${response.status})`
    throw new ApiError(reason, response.status, detail)
  }
  return body as T
}

export const api = {
  tickets: () => request<TicketsResponse>('/api/tickets'),

  cash: () => request<CashResponse>('/api/cash'),

  /** Events newer than `sinceSeq`, so the feed polls deltas rather than the whole trail. */
  events: (sinceSeq: number, limit = 200) =>
    request<EventsResponse>(`/api/events?since_seq=${sinceSeq}&limit=${limit}`),

  runTicket: (ticketId: number) =>
    request<{ run_id: string; ticket_id: number; status: string }>(
      `/api/tickets/${ticketId}/run`,
      { method: 'POST', body: JSON.stringify({ wait: false }) },
    ),

  runState: (runId: string) => request<RunState>(`/api/runs/${runId}`),

  /** The only call that moves money. `approvedBy` is the human who clicked. */
  approvePayment: (payload: {
    kind: 'invoice' | 'rent'
    ref_id: number
    amount: number
    approved_by: string
    ticket_id: number | null
  }) =>
    request<ApprovalResult>('/api/payments/approve', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  reset: () => request<{ reset: boolean; balance: number }>('/api/reset', { method: 'POST' }),

  health: () =>
    request<{ ok: boolean; mcp_reachable: boolean; desk_date: string | null }>('/api/health'),
}
