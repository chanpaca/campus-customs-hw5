import { useEffect, useRef } from 'react'
import type { AgentEvent, AgentName } from '../types'

const AGENT_LABEL: Record<AgentName, string> = {
  boss: 'Boss',
  inventory: 'Inventory',
  accounting: 'Accounting',
  facilities: 'Facilities',
  customer_service: 'Cust. Service',
}

const KIND_LABEL: Record<string, string> = {
  run_started: 'run started',
  agent_started: 'thinking',
  delegation: 'hand-off',
  delegation_refused: 'hand-off refused',
  tool_call: 'tool call',
  agent_finished: 'concluded',
  voucher_prepared: 'voucher prepared',
  human_approval_required: 'needs a human',
  budget_exhausted: 'budget spent',
  error: 'error',
  run_finished: 'run finished',
}

function time(iso: string): string {
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? '' : d.toLocaleTimeString([], { hour12: false })
}

/** One line of the tool result, enough to see the answer without the noise. */
function toolGist(event: AgentEvent): string | null {
  const r = event.tool_result as Record<string, unknown> | null
  if (!r || typeof r !== 'object') return null

  const keys = [
    'balance',
    'shortfall',
    'qty_on_hand',
    'amount',
    'days_past_due',
    'days_until_due',
    'ticket_count',
    'invoice_count',
    'lease_count',
    'recorded',
    'drafted',
    'updated',
    'rejected_reason',
    'blocked_reason',
    'not_found_reason',
  ]
  const parts: string[] = []
  for (const key of keys) {
    if (r[key] !== undefined && r[key] !== null) parts.push(`${key}: ${String(r[key])}`)
    if (parts.length === 3) break
  }
  return parts.length ? parts.join(' · ') : null
}

function EventRow({ event }: { event: AgentEvent }) {
  const agent = event.agent ?? null
  const tagClass = agent ? `agent-${agent}` : 'agent-system'
  const tagText = agent ? AGENT_LABEL[agent] : 'Desk'
  const rowClass = [
    'event',
    !event.ok || event.event === 'error' ? 'is-error' : '',
    event.event === 'human_approval_required' || event.event === 'voucher_prepared'
      ? 'is-approval'
      : '',
  ]
    .filter(Boolean)
    .join(' ')

  return (
    <div className={rowClass}>
      <span className={`agent-tag ${tagClass}`}>{tagText}</span>
      <div className="event-main">
        <div className="event-kind">
          <span>{KIND_LABEL[event.event] ?? event.event}</span>
          <time dateTime={event.at}>{time(event.at)}</time>
          {event.ticket_id != null && <span>#{event.ticket_id}</span>}
        </div>

        {event.event === 'delegation' || event.event === 'delegation_refused' ? (
          <p className="event-text">
            <span className="handoff">
              {agent ? AGENT_LABEL[agent] : 'Desk'}
              <span className="arrow">→</span>
              {event.to_agent ? AGENT_LABEL[event.to_agent] : '?'}
            </span>
            {event.message && <span className="quiet"> — {event.message}</span>}
          </p>
        ) : event.event === 'tool_call' ? (
          <>
            <p className="event-text">
              <span className="tool-pill">{event.tool_name}</span>
              {event.tool_args && Object.keys(event.tool_args).length > 0 && (
                <span className="tool-result"> {JSON.stringify(event.tool_args)}</span>
              )}
            </p>
            {toolGist(event) && <div className="tool-result">↳ {toolGist(event)}</div>}
          </>
        ) : (
          <p className="event-text quiet">{event.message ?? '—'}</p>
        )}
      </div>
    </div>
  )
}

interface Props {
  events: AgentEvent[]
  isRunning: boolean
}

export function ActivityFeed({ events, isRunning }: Props) {
  const endRef = useRef<HTMLDivElement>(null)

  // Follow the conversation while the team is working, the way you would watch
  // a terminal. Once it stops, leave the scroll position alone so the reader
  // can go back through what happened.
  useEffect(() => {
    if (isRunning) endRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
  }, [events.length, isRunning])

  return (
    <section className="panel">
      <div className="panel-head">
        <h2>Live Agent Activity</h2>
        <span className="count">{events.length} events</span>
      </div>
      <div className="panel-body flush feed">
        {events.length === 0 ? (
          <div className="feed-empty">
            <b>The desk is quiet.</b>
            Pick a ticket and run the agent team to watch them work.
          </div>
        ) : (
          events.map((event) => <EventRow key={event.index} event={event} />)
        )}
        <div ref={endRef} />
      </div>
    </section>
  )
}
