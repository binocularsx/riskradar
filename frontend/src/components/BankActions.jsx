import { useCallback } from 'react'
import { api, when } from '../lib/api'
import { usePolling } from '../lib/usePolling'
import ReadStatus from './ReadStatus'
import { BANK_ACTION, CHANNEL, DELIVERY, say } from '../lib/words'

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
      const [label, tone] = STATUS[r.status] || [say(DELIVERY, r.status) || 'Status not known', '']
      return <article className="bank-action" key={r.restriction_ref}>
        <strong>{say(BANK_ACTION, r.action)}</strong>
        <span className={`pill ${tone}`}>{label}</span>
        {r.channel && <p>Channel: {say(CHANNEL, r.channel)}</p>}
        {r.transaction_ref && <p className="mono">Payment: {r.transaction_ref}</p>}
        {r.reason && <p>{r.reason}</p>}
        <details><summary>Request reference and dates</summary><p className="mono">{r.restriction_ref}</p><p>Issued {when(r.issued_at)}<br />Bank outcome received {when(r.acknowledged_at)}</p></details>
      </article>
    })}
    {!!items?.length && <p className="dim">These are reported request outcomes. Check the bank record for the current restriction or release state.</p>}
    {resource.updatedAt && <small className="dim">Last read {when(resource.updatedAt)} · refreshes every 20s</small>}
  </section>
}
