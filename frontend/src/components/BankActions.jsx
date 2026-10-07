import { useCallback } from 'react'
import { api, when } from '../lib/api'
import { usePolling } from '../lib/usePolling'
import ReadStatus from './ReadStatus'

const ACTIONS = {
  DEBIT_RESTRICTION: 'Debit restriction', CHANNEL_RESTRICTION: 'Channel restriction',
  CARD_FREEZE: 'Card freeze', BENEFICIARY_RESTRICTION: 'Destination restriction',
  TRANSACTION_REVERSAL: 'Transaction reversal', SESSION_TERMINATION: 'Session termination',
  CREDENTIAL_RESET: 'Credential reset', MFA_REENROLMENT: 'MFA re-enrolment',
}
const STATUS = {
  RECOMMENDED: ['Approved request · awaiting delivery', 'escalate'],
  DELIVERED: ['Delivered · awaiting bank outcome', 'escalate'],
  APPLIED: ['Bank reports applied', 'suppress'],
  NOT_APPLIED: ['Bank reports not applied', 'override'],
  REJECTED: ['Rejected by bank', 'override'],
}

export default function BankActions({ caseId }) {
  const load = useCallback(() => api.caseRestrictions(caseId), [caseId])
  const resource = usePolling(load)
  const items = resource.data?.items
  return <section className="card bank-actions" id="case-bank-actions" aria-labelledby="case-bank-actions-title">
    <h3 id="case-bank-actions-title">Bank action requests</h3>
    <p className="dim">A lead approves the request; the bank applies it and reports the outcome.</p>
    <ReadStatus resource={resource} label="Bank action requests" />
    {items?.length === 0 && <p className="dim">No approved bank action requests recorded for this case.</p>}
    {items?.map((r) => {
      const [label, tone] = STATUS[r.status] || [r.status || 'Unknown status', '']
      return <article className="bank-action" key={r.restriction_ref}>
        <strong>{ACTIONS[r.action] || r.action.replace(/_/g, ' ').toLowerCase()}</strong>
        <span className={`pill ${tone}`}>{label}</span>
        {r.channel && <p>Channel: {r.channel.replace(/_/g, ' ').toLowerCase()}</p>}
        {r.transaction_ref && <p className="mono">Payment: {r.transaction_ref}</p>}
        {r.reason && <p>{r.reason}</p>}
        <details><summary>Request reference and dates</summary><p className="mono">{r.restriction_ref}</p><p>Issued {when(r.issued_at)}<br />Bank outcome received {when(r.acknowledged_at)}</p></details>
      </article>
    })}
    {!!items?.length && <p className="dim">These are reported request outcomes. Check the bank record for the current restriction or release state.</p>}
    {resource.updatedAt && <small className="dim">Last read {when(resource.updatedAt)} · refreshes every 20s</small>}
  </section>
}
