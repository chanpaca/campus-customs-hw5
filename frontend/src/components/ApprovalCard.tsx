import { useState } from 'react'
import { ApiError, api } from '../api'
import type { ApprovalResult, Voucher } from '../types'

const money = (n: number) =>
  n.toLocaleString('en-US', { style: 'currency', currency: 'USD' })

interface Props {
  voucher: Voucher
  approver: string
  onApproved: (result: ApprovalResult) => void
}

/**
 * The human-in-the-loop moment.
 *
 * Agents can only ever draft a voucher; this card is the one place in the whole
 * system where money actually moves, so it states the amount, the payee, and
 * the resulting balance before asking — and it refuses to submit without a
 * named approver, because `approved_by` is the audit record of who decided.
 */
export function ApprovalCard({ voucher, approver, onApproved }: Props) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [done, setDone] = useState<ApprovalResult | null>(null)

  const nameMissing = approver.trim().length < 2

  async function approve() {
    setBusy(true)
    setError(null)
    try {
      const result = await api.approvePayment({
        kind: voucher.kind,
        ref_id: voucher.ref_id,
        amount: voucher.amount,
        approved_by: approver.trim(),
        ticket_id: voucher.ticket_id,
      })
      setDone(result)
      onApproved(result)
    } catch (err) {
      // A refusal here is the guardrail working, so show its own words rather
      // than a generic failure message.
      setError(err instanceof ApiError ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <article className="approval">
      <div className="approval-head">
        <span className="title">
          {done ? 'Payment recorded' : 'Awaiting your approval'}
        </span>
        <span className="mono">{voucher.voucher_id}</span>
      </div>

      <div className="approval-body">
        <div className="approval-amount">{money(voucher.amount)}</div>
        <div className="approval-payee">
          to <b>{voucher.payee ?? 'unknown payee'}</b>
          {voucher.memo ? ` — ${voucher.memo}` : ''}
        </div>

        <div className="approval-meta">
          <div>
            <span>Kind</span>
            <strong>{voucher.kind === 'invoice' ? `invoice ${voucher.ref_id}` : `lease ${voucher.ref_id}`}</strong>
          </div>
          {voucher.due_date && (
            <div>
              <span>Due</span>
              <strong>{voucher.due_date}</strong>
            </div>
          )}
          {voucher.days_past_due != null && (
            <div>
              <span>{voucher.days_past_due > 0 ? 'Days overdue' : 'Days until due'}</span>
              <strong>{Math.abs(voucher.days_past_due)}</strong>
            </div>
          )}
          {voucher.balance_after_if_approved != null && (
            <div>
              <span>Balance after</span>
              <strong>{money(voucher.balance_after_if_approved)}</strong>
            </div>
          )}
        </div>

        <p className="approval-note">
          Prepared by the {voucher.preparedBy?.replace('_', ' ') ?? 'agent'} agent. Agents cannot
          approve their own vouchers — this payment only reaches the ledger when you sign it.
        </p>

        {done ? (
          <div className="approval-done">
            Paid {money(done.amount)} · approved by {done.approved_by} · balance{' '}
            {money(done.balance_before)} → {money(done.balance_after)}
          </div>
        ) : (
          <>
            <button className="btn btn-approve" onClick={approve} disabled={busy || nameMissing}>
              {busy ? 'Recording payment…' : `Approve Payment · ${money(voucher.amount)}`}
            </button>
            {nameMissing && (
              <p className="approval-note" style={{ marginTop: 10, marginBottom: 0 }}>
                Enter your name in the header before approving.
              </p>
            )}
          </>
        )}

        {error && <div className="approval-error">Refused — {error}</div>}
      </div>
    </article>
  )
}
