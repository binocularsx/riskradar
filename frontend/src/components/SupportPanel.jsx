import { useCallback, useState } from 'react'
import { api, when } from '../lib/api'
import { usePolling } from '../lib/usePolling'
import { DELIVERY, OUTCOME, SUPPORT_MESSAGE, say } from '../lib/words'
import ReadStatus from './ReadStatus'
import { Banner } from './ui'

/**
 * What the support team has been told about this case (D109).
 *
 * The fraud desk is not customer-facing: it recommends, and the support team
 * acts on the customer. This shows every message that has gone to support —
 * the urgent heads-up, the lead-approved report, a request to contact a
 * watch-flagged customer — and whether it landed. On a Critical case the
 * analyst working it, or a lead, can send support one urgent heads-up before
 * the report is approved.
 */
export default function SupportPanel({ summary, user, onSent }) {
  const load = useCallback(() => api.caseReports(summary.id), [summary.id])
  const resource = usePolling(load)
  const items = resource.data?.items
  const [message, setMessage] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  const sentHeadsUp = items?.some((m) => m.kind === 'HEADS_UP')
  const mayAlert = summary.risk_level === 'CRITICAL' && summary.state !== 'CLOSED' && !summary.outcome
    && (summary.assignee_id === user.id || user.role === 'FRAUD_OPS_LEAD')

  async function send() {
    setBusy(true); setError(null)
    try {
      await api.headsUp(summary.id, message.trim())
      setMessage('')
      await resource.refresh()
      onSent?.()
    } catch (e) { setError(e.message) } finally { setBusy(false) }
  }

  return <section className="card" id="case-support" aria-labelledby="case-support-title">
    <h3 id="case-support-title">Support team</h3>
    <p className="dim">The fraud desk recommends; the support team contacts the customer and acts on their profile.
      {summary.support_ticket_ref && <> This case came from support ticket <strong>{summary.support_ticket_ref}</strong>.</>}</p>
    <ReadStatus resource={resource} label="Messages to support" />
    {items?.length === 0 && <p className="dim">Nothing sent to support yet. The report goes once a lead approves the finding.</p>}
    {items?.map((m) => <article className="bank-action" key={m.report_ref}>
      <strong>{say(SUPPORT_MESSAGE, m.kind)}{m.outcome ? `: ${say(OUTCOME, m.outcome).toLowerCase()}` : ''}</strong>
      <span className={`pill ${m.status === 'SENT' ? 'suppress' : m.status === 'FAILED' ? 'override' : 'escalate'}`}>
        {say(DELIVERY, m.status)}
      </span>
      <p className="dim">Created {when(m.created_at)}{m.sent_at ? ` · sent ${when(m.sent_at)}` : ''}</p>
    </article>)}

    {mayAlert && !sentHeadsUp && <div style={{ marginTop: 10 }}>
      <p style={{ fontSize: 12.5, margin: '0 0 6px' }}>
        <strong>Money still leaving?</strong> Send support an urgent heads-up to hold transfers while you finish.
        The full report still waits for a lead.
      </p>
      {error && <Banner kind="error">{error}</Banner>}
      <textarea style={{ minHeight: 50 }} value={message} onChange={(e) => setMessage(e.target.value)}
                placeholder="What support needs to know now (at least 20 characters)" />
      <button className="primary" style={{ marginTop: 6 }} disabled={busy || message.trim().length < 20} onClick={send}>
        Send urgent heads-up to support
      </button>
    </div>}
  </section>
}
