import { statusLabel } from './TicketBoard'
import type { AgentName, RunResult } from '../types'

const AGENT_LABEL: Record<AgentName, string> = {
  boss: 'Boss',
  inventory: 'Inventory',
  accounting: 'Accounting',
  facilities: 'Facilities',
  customer_service: 'Cust. Service',
}

interface Props {
  result: RunResult
}

/** What the team concluded, once the run is over. */
export function TicketSummary({ result }: Props) {
  const output = result.boss_output
  const status = result.final_state?.status ?? 'in_progress'
  const badge = statusLabel(status, false)
  const accepted = result.delegations.filter((d) => d.accepted)

  return (
    <section className="panel">
      <div className="panel-head">
        <h2>Ticket Summary</h2>
        <span className="count">ticket #{result.ticket_id}</span>
      </div>
      <div className="panel-body">
        {result.error ? (
          <div className="banner err">
            <b>The run did not complete.</b> {result.error}
          </div>
        ) : null}

        <div className="summary-head">
          <span className={`badge ${badge.cls}`}>{badge.text}</span>
          {result.final_state?.needs_human && (
            <span className="badge awaiting">Human action required</span>
          )}
          <span className="count mono">{result.model}</span>
        </div>

        {output && <p className="summary-statement">{output.summary}</p>}

        <div className="summary-grid">
          <div className="stat">
            <span>Hand-offs</span>
            <strong>{accepted.length}</strong>
          </div>
          <div className="stat">
            <span>Turns used</span>
            <strong>
              {result.budget.turns_used}/{result.budget.max_delegation_turns}
            </strong>
          </div>
          <div className="stat">
            <span>MCP calls</span>
            <strong>{result.tool_invocations.length}</strong>
          </div>
          <div className="stat">
            <span>Vouchers</span>
            <strong>{output?.prepared_vouchers.length ?? 0}</strong>
          </div>
        </div>

        {accepted.length > 0 && (
          <div className="summary-section">
            <h3>Delegation chain</h3>
            <div className="chain">
              <span className="badge resolving">Boss</span>
              {accepted.map((d, i) => (
                <span key={i} className="chain">
                  <span className="arrow">→</span>
                  <span className={`agent-tag agent-${d.delegation.to_agent}`}>
                    {AGENT_LABEL[d.delegation.to_agent]}
                  </span>
                </span>
              ))}
            </div>
          </div>
        )}

        {output && output.facts_used.length > 0 && (
          <div className="summary-section">
            <h3>Facts it relied on</h3>
            <ul>
              {output.facts_used.map((f, i) => (
                <li key={i}>
                  {f.fact} <span className="src">({f.source_tool})</span>
                </li>
              ))}
            </ul>
          </div>
        )}

        {output && output.blocking_issues.length > 0 && (
          <div className="summary-section">
            <h3>Blocking issues</h3>
            <ul>
              {output.blocking_issues.map((b, i) => (
                <li key={i}>{b}</li>
              ))}
            </ul>
          </div>
        )}

        {output && output.recommended_actions.length > 0 && (
          <div className="summary-section">
            <h3>Recommended next steps</h3>
            <ul>
              {output.recommended_actions.map((a, i) => (
                <li key={i}>{a}</li>
              ))}
            </ul>
          </div>
        )}

        {output?.customer_message && (
          <div className="summary-section">
            <h3>Message drafted for the customer</h3>
            <p className="summary-statement">{output.customer_message}</p>
          </div>
        )}
      </div>
    </section>
  )
}
