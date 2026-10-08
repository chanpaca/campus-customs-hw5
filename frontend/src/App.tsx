import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { ApiError, api } from './api'
import { ActivityFeed } from './components/ActivityFeed'
import { ApprovalCard } from './components/ApprovalCard'
import { TicketBoard } from './components/TicketBoard'
import { TicketSummary } from './components/TicketSummary'
import type {
  AgentEvent,
  ApprovalResult,
  CashResponse,
  RunResult,
  Ticket,
  Voucher,
} from './types'

const money = (n: number) =>
  n.toLocaleString('en-US', { style: 'currency', currency: 'USD' })

const POLL_MS = 1200

/**
 * Rebuild the pending vouchers from the event stream.
 *
 * The draft tool's own result carries the payee, amount, due date and projected
 * balance, so the card shows the agent's actual figures rather than anything
 * the UI invented. Vouchers already settled are dropped by matching them
 * against the payments ledger.
 */
function vouchersFromEvents(events: AgentEvent[], paidRefs: Set<string>): Voucher[] {
  const found = new Map<string, Voucher>()

  for (const event of events) {
    if (event.event !== 'tool_call' || event.tool_name !== 'draft_payment_voucher') continue
    const r = event.tool_result as Record<string, unknown> | null
    if (!r || r.drafted !== true) continue

    const voucher: Voucher = {
      voucher_id: String(r.voucher_id ?? `VCH-${r.kind}-${r.ref_id}`),
      kind: r.kind === 'rent' ? 'rent' : 'invoice',
      ref_id: Number(r.ref_id),
      payee: (r.payee as string) ?? null,
      amount: Number(r.amount ?? 0),
      memo: (r.memo as string) ?? null,
      due_date: (r.due_date as string) ?? null,
      days_past_due: r.days_past_due == null ? null : Number(r.days_past_due),
      balance_before: r.balance_before == null ? null : Number(r.balance_before),
      balance_after_if_approved:
        r.balance_after_if_approved == null ? null : Number(r.balance_after_if_approved),
      ticket_id: event.ticket_id,
      preparedBy: event.agent,
      seq: event.index,
    }
    found.set(`${voucher.kind}:${voucher.ref_id}`, voucher)
  }

  return [...found.entries()]
    .filter(([key]) => !paidRefs.has(key))
    .map(([, voucher]) => voucher)
    .sort((a, b) => a.seq - b.seq)
}

export default function App() {
  const [tickets, setTickets] = useState<Ticket[]>([])
  const [deskDate, setDeskDate] = useState<string>('')
  const [cash, setCash] = useState<CashResponse | null>(null)
  const [cashDelta, setCashDelta] = useState<string>('')
  const [cashDropped, setCashDropped] = useState(false)

  const [selectedId, setSelectedId] = useState<number | null>(null)
  const [runningId, setRunningId] = useState<number | null>(null)
  const [runId, setRunId] = useState<string | null>(null)
  const [runResult, setRunResult] = useState<RunResult | null>(null)

  const [events, setEvents] = useState<AgentEvent[]>([])
  const lastSeq = useRef(0)
  const [error, setError] = useState<string | null>(null)
  const [approver, setApprover] = useState(
    () => localStorage.getItem('cc.approver') ?? '',
  )

  useEffect(() => {
    localStorage.setItem('cc.approver', approver)
  }, [approver])

  // ---------------------------------------------------------------- loaders

  const loadTickets = useCallback(async () => {
    try {
      const data = await api.tickets()
      setTickets(data.tickets)
      setDeskDate(data.as_of)
      setSelectedId((current) => current ?? data.tickets[0]?.ticket_id ?? null)
      // Clear a stale banner: if the API answers now, whatever failed before
      // (typically a 502 while the backend was still booting) is no longer true.
      setError(null)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err))
    }
  }, [])

  const loadCash = useCallback(async () => {
    try {
      setCash(await api.cash())
      setError(null)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err))
    }
  }, [])

  useEffect(() => {
    void loadTickets()
    void loadCash()

    // Start the feed from "now". The audit trail is append-only and holds every
    // earlier run, so replaying it on load would bury today's work under last
    // week's. Fast-forward past what already exists and show only new activity.
    void api
      .events(0, 1)
      .then((batch) => {
        lastSeq.current = batch.total_events
      })
      .catch(() => {
        /* the poll below will catch up */
      })
  }, [loadTickets, loadCash])

  // ------------------------------------------------------- the live feed

  useEffect(() => {
    // Poll quickly while a run is in flight, lazily when the desk is idle.
    const interval = window.setInterval(
      async () => {
        try {
          const batch = await api.events(lastSeq.current)
          if (batch.events.length) {
            lastSeq.current = batch.last_seq
            setEvents((current) => [...current, ...batch.events].slice(-400))
          }
          if (runId) {
            const state = await api.runState(runId)
            if (state.status !== 'running') {
              setRunResult(state.result)
              setRunningId(null)
              setRunId(null)
              void loadTickets()
              void loadCash()
            }
          }
        } catch {
          /* a dropped poll is not worth surfacing; the next tick retries */
        }
      },
      runId ? POLL_MS : POLL_MS * 4,
    )
    return () => window.clearInterval(interval)
  }, [runId, loadTickets, loadCash])

  // ------------------------------------------------------------- actions

  async function runTeam() {
    if (selectedId == null) return
    setError(null)
    setRunResult(null)
    setEvents([])
    try {
      lastSeq.current = (await api.events(0, 1)).total_events
      const started = await api.runTicket(selectedId)
      setRunId(started.run_id)
      setRunningId(selectedId)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err))
    }
  }

  async function resetDesk() {
    setError(null)
    try {
      await api.reset()
      setRunResult(null)
      setEvents([])
      // Fast-forward past the reset's own events; the trail is append-only, so
      // rewinding the cursor to 0 would replay every earlier run into the feed.
      lastSeq.current = (await api.events(0, 1)).total_events
      await loadTickets()
      await loadCash()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err))
    }
  }

  function onApproved(result: ApprovalResult) {
    // The approval response already carries the authoritative post-payment
    // balance, so apply it immediately rather than making the operator watch a
    // stale number while /api/cash spawns an MCP subprocess (3-5s). The refetch
    // that follows reconciles the ledger and confirms what we just drew.
    setCash((previous) =>
      previous
        ? { ...previous, balance: result.balance_after }
        // No balance loaded yet (slow first fetch): the approval response is
        // still authoritative, so show it rather than leaving a dash on screen.
        : {
            as_of: '',
            found: true,
            account: 'checking',
            balance: result.balance_after,
            balance_as_of: '',
            payments_recorded: [],
            total_disbursed: result.balance_before - result.balance_after,
          },
    )
    setCashDelta(`− ${money(result.balance_before - result.balance_after)}`)
    setCashDropped(true)
    window.setTimeout(() => setCashDropped(false), 1200)
    window.setTimeout(() => setCashDelta(''), 5000)

    void loadCash()
    void loadTickets()
  }

  // ------------------------------------------------------------- derived

  const paidRefs = useMemo(
    () => new Set((cash?.payments_recorded ?? []).map((p) => `${p.kind}:${p.ref_id}`)),
    [cash],
  )
  const vouchers = useMemo(() => vouchersFromEvents(events, paidRefs), [events, paidRefs])
  const selected = tickets.find((t) => t.ticket_id === selectedId) ?? null

  return (
    <>
      <header className="masthead">
        <div className="masthead-inner">
          <div className="wordmark">
            <h1>Campus Customs</h1>
            <span className="desk">Operations Desk</span>
          </div>

          <div className="masthead-right">
            <div className="approver">
              <label htmlFor="approver">Approver on duty</label>
              <input
                id="approver"
                value={approver}
                placeholder="your name"
                onChange={(e) => setApprover(e.target.value)}
              />
            </div>

            <div className="deskdate">
              Desk date
              <b>{deskDate || '—'}</b>
            </div>

            <div className="cash">
              <span className="label">Checking</span>
              <span className={`amount${cashDropped ? ' dropped' : ''}`}>
                {cash ? money(cash.balance) : '—'}
              </span>
              <span className="delta">{cashDelta}</span>
            </div>
          </div>
        </div>
      </header>

      <main className="shell">
        {error && <div className="banner err">{error}</div>}

        <div className="columns">
          <div>
            <TicketBoard
              tickets={tickets}
              selectedId={selectedId}
              runningId={runningId}
              onSelect={(id) => setSelectedId(id)}
            />

            <section className="panel">
              <div className="panel-head">
                <h2>Actions</h2>
              </div>
              <div className="panel-body actions">
                <p className="selected-line">
                  {selected ? (
                    <>
                      Selected: <b>#{selected.ticket_id}</b> — {selected.subject}
                    </>
                  ) : (
                    'Select a ticket from the board.'
                  )}
                </p>
                <button
                  className="btn btn-primary"
                  onClick={runTeam}
                  disabled={selectedId == null || runningId != null}
                >
                  {runningId != null ? 'Agent team working…' : 'Run Agent Team'}
                </button>
                <button className="btn btn-ghost" onClick={resetDesk} disabled={runningId != null}>
                  Reset desk to $3,400.00
                </button>
              </div>
            </section>

            {cash && cash.payments_recorded.length > 0 && (
              <section className="panel">
                <div className="panel-head">
                  <h2>Payments Ledger</h2>
                  <span className="count">{money(cash.total_disbursed)} out</span>
                </div>
                <div className="panel-body">
                  <ul style={{ margin: 0, paddingLeft: 18 }}>
                    {cash.payments_recorded.map((p) => (
                      <li key={p.payment_id}>
                        {money(p.amount)} · {p.kind} {p.ref_id} ·{' '}
                        <span className="mono">approved by {p.approved_by}</span>
                      </li>
                    ))}
                  </ul>
                </div>
              </section>
            )}
          </div>

          <div>
            {vouchers.length > 0 && (
              <section className="panel" style={{ background: 'transparent', border: 0, boxShadow: 'none' }}>
                {vouchers.map((voucher) => (
                  <ApprovalCard
                    key={voucher.voucher_id}
                    voucher={voucher}
                    approver={approver}
                    onApproved={onApproved}
                  />
                ))}
              </section>
            )}

            {runResult && <TicketSummary result={runResult} />}

            <ActivityFeed events={events} isRunning={runningId != null} />
          </div>
        </div>
      </main>

      <footer className="deskfoot">
        <span>Agents prepare. Humans approve.</span>
        <span>Every figure read from the Campus Customs database through MCP.</span>
      </footer>
    </>
  )
}
