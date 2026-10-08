import type { Ticket, TicketStatus } from '../types'

/**
 * Status label shown on a ticket.
 *
 * `in_progress` reads as "Resolving" because that is what a desk manager cares
 * about — the team is on it — and `awaiting_approval` gets its own amber badge
 * rather than hiding inside "Resolving", since it means *you* have to act.
 */
export function statusLabel(status: TicketStatus, isRunning: boolean): {
  text: string
  cls: string
} {
  if (isRunning) return { text: 'Resolving', cls: 'resolving' }
  switch (status) {
    case 'resolved':
      return { text: 'Resolved', cls: 'resolved' }
    case 'awaiting_approval':
      return { text: 'Needs Approval', cls: 'awaiting' }
    case 'in_progress':
      return { text: 'Resolving', cls: 'resolving' }
    case 'blocked':
      return { text: 'Blocked', cls: 'blocked' }
    default:
      return { text: 'Open', cls: 'open' }
  }
}

interface Props {
  tickets: Ticket[]
  selectedId: number | null
  runningId: number | null
  onSelect: (id: number) => void
}

export function TicketBoard({ tickets, selectedId, runningId, onSelect }: Props) {
  return (
    <section className="panel">
      <div className="panel-head">
        <h2>Ticket Board</h2>
        <span className="count">{tickets.length} on the desk</span>
      </div>
      <div className="panel-body flush">
        {tickets.map((ticket) => {
          const badge = statusLabel(ticket.status, runningId === ticket.ticket_id)
          return (
            <button
              key={ticket.ticket_id}
              className="ticket"
              aria-pressed={selectedId === ticket.ticket_id}
              onClick={() => onSelect(ticket.ticket_id)}
            >
              <div className="ticket-top">
                <span className="ticket-id">#{ticket.ticket_id} · {ticket.type}</span>
                <span className={`badge ${badge.cls}`}>{badge.text}</span>
              </div>
              <div className="ticket-subject">{ticket.subject}</div>
              <div className="ticket-requester">{ticket.requester}</div>
              <div className="ticket-facts">
                {ticket.sku && <span className="fact">{ticket.sku}</span>}
                {ticket.size && <span className="fact">size {ticket.size}</span>}
                {ticket.qty != null && <span className="fact">qty {ticket.qty}</span>}
                {ticket.invoice_id != null && <span className="fact">invoice {ticket.invoice_id}</span>}
                {ticket.lease_id != null && <span className="fact">lease {ticket.lease_id}</span>}
              </div>
            </button>
          )
        })}
      </div>
    </section>
  )
}
