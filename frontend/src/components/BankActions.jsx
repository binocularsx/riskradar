import { useCallback } from 'react'
import { api, when } from '../lib/api'
import { usePolling } from '../lib/usePolling'
import ReadStatus from './ReadStatus'
import { BANK_ACTION, CHANNEL, DELIVERY, say } from '../lib/words'

const STATUS = {
  RECOMMENDED: ['Approved · waiting to reach support', 'escalate'],
  DELIVERED: ['With support · waiting for their reply', 'escalate'],
  APPLIED: ['Support has done it', 'suppress'],
  NOT_APPLIED: ['Support did not do it', 'override'],
  REJECTED: ['Support refused it', 'override'],
}

export default function BankActions({ caseId }) {
  const load = useCallback(() => api.caseRestrictions(caseId), [caseId])
  const resource = usePolling(load)
  const items = resource.data?.items
  return <section className="card bank-actions" id="case-bank-actions" aria-labelledby="case-bank-actions-title">
    <h3 id="case-bank-actions-title">Actions asked of support</h3>
    <p className="dim">A lead approves them; the support team carries them out on the customer's profile and confirms each one.</p>
    <ReadStatus resource={resource} label="Actions asked of support" />
    {items?.length === 0 && <p className="dim">No actions asked of support on this case yet.</p>}
    {items?.map((r) => {
      const [label, tone] = STATUS[r.status] || [say(DELIVERY, r.status) || 'Status not known', '']
      return <article className="bank-action" key={r.restriction_ref}>
        <strong>{say(BANK_ACTION, r.action)}</strong>
        <span className={`pill ${tone}`}>{label}</span>
        {r.channel && <p>Channel: {say(CHANNEL, r.channel)}</p>}
        {r.transaction_ref && <p className="mono">Payment: {r.transaction_ref}</p>}
        {r.reason && <p>{r.reason}</p>}
        <details><summary>Request reference and dates</summary><p className="mono">{r.restriction_ref}</p><p>Issued {when(r.issued_at)}<br />Support replied {when(r.acknowledged_at)}</p></details>
      </article>
    })}
    {!!items?.length && <p className="dim">These are what support reported back. Check the customer's profile for its current state.</p>}
    {resource.updatedAt && <small className="dim">Last read {when(resource.updatedAt)} · refreshes every 20s</small>}
  </section>
}
