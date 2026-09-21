import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'

import { api } from '../lib/api'
import { Banner } from '../components/ui'

/**
 * The lead's queue (D93).
 *
 * An analyst proposes a fraud finding; a different lead decides it. This is
 * that decision, and it deliberately shows four things before the buttons:
 * who proposed it, what they say, what it would do to the customer, and how
 * long it has waited — because approving without reading is the failure mode
 * a second pair of eyes exists to prevent.
 *
 * A lead's own proposals never appear here. The server enforces that; this
 * page says so, so nobody wonders where their own submission went.
 */
const DECISIONS = [
  { key: 'APPROVE', label: 'Approve', cls: 'confirm', hint: 'the finding stands; any restriction is authorised' },
  { key: 'REJECT', label: 'Reject', cls: 'dismiss', hint: 'the finding does not stand; the case stays open' },
  { key: 'RETURN', label: 'Return', cls: 'unsure', hint: 'send it back with what is missing' },
]

function money(minor, currency = 'NGN') {
  return `${currency} ${(Number(minor || 0) / 100).toLocaleString()}`
}

function waited(seconds) {
  const m = Math.round((seconds || 0) / 60)
  if (m < 60) return `${m} min`
  const h = Math.floor(m / 60)
  return `${h}h ${m % 60}m`
}

export default function Approvals() {
  const [queue, setQueue] = useState(null)
  const [reasons, setReasons] = useState({})
  const [busy, setBusy] = useState(null)
  const [error, setError] = useState(null)
  const [flash, setFlash] = useState(null)

  const load = useCallback(async () => {
    try { setQueue(await api.approvals()); setError(null) } catch (e) { setError(e.message) }
  }, [])

  useEffect(() => {
    load()
    const timer = setInterval(load, 10000)
    return () => clearInterval(timer)
  }, [load])

  const decide = useCallback(async (item, decision) => {
    const reason = (reasons[item.id] || '').trim()
    if (reason.length < 10) {
      setError('Say why in at least 10 characters: your decision is recorded with its reason.')
      return
    }
    setBusy(item.id); setError(null)
    try {
      await api.decideSubmission(item.id, { decision, reason })
      setFlash(`Case #${item.case_id}: ${decision.toLowerCase()}d.`)
      setReasons((r) => ({ ...r, [item.id]: '' }))
      await load()
    } catch (e) { setError(e.message) } finally { setBusy(null) }
  }, [reasons, load])

  if (!queue) return <p className="muted">Loading…</p>
  const { items, summary } = queue

  return (
    <div className="page">
      <header className="page-head">
        <h1>Fraud approvals</h1>
        <p className="dim">
          An analyst proposes; you decide. You cannot decide your own proposals, and only an approval
          writes the outcome onto the case or authorises a restriction.
        </p>
      </header>

      {error && <Banner kind="error">{error}</Banner>}
      {flash && <Banner kind="ok">{flash}</Banner>}

      <div className="row wrap" style={{ gap: 18, margin: '4px 0 14px' }}>
        <span><strong>{summary.waiting}</strong> waiting</span>
        <span className="dim">oldest {waited(summary.oldest_seconds)}</span>
        <span className="dim">{summary.with_restrictions} asking to restrict an account</span>
        {summary.mine_awaiting_someone_else > 0 && (
          <span className="dim">{summary.mine_awaiting_someone_else} of yours waiting for another lead</span>
        )}
      </div>

      {items.length === 0 && <p className="muted">Nothing waiting for a decision.</p>}

      {items.map((item) => (
        <section key={item.id} className="card" style={{ marginBottom: 14 }}>
          <div className="between wrap">
            <div>
              <strong>Case #{item.case_id}</strong>{' '}
              <span className={`pill ${String(item.risk_level).toLowerCase()}`}>{item.risk_level}</span>{' '}
              <span className="dim">{item.alert_count} alert(s) · {money(item.exposure_minor)} at risk</span>
            </div>
            <div className="dim">
              proposed by {item.submitted_by_name} · waiting {waited(item.waiting_seconds)}
            </div>
          </div>

          <p style={{ margin: '10px 0 4px' }}>
            Proposes <strong>{String(item.proposed_outcome).replace(/_/g, ' ').toLowerCase()}</strong>
          </p>
          <p className="dim" style={{ whiteSpace: 'pre-wrap', margin: '0 0 8px' }}>{item.rationale}</p>

          {item.restrictions?.length > 0 && (
            <div className="warn-box" style={{ marginBottom: 8 }}>
              <strong>Asks the bank to act on the customer:</strong>
              <ul style={{ margin: '6px 0 0 18px' }}>
                {item.restrictions.map((r, i) => (
                  <li key={i}>
                    {String(r.action).replace(/_/g, ' ').toLowerCase()}
                    {r.channel ? ` (${r.channel})` : ''}
                    {r.reason ? ` — ${r.reason}` : ''}
                  </li>
                ))}
              </ul>
              <p className="dim" style={{ margin: '6px 0 0', fontSize: 12 }}>
                Approving authorises this. Nothing is sent to the bank until the restriction interface exists.
              </p>
            </div>
          )}

          <textarea style={{ minHeight: 46 }} value={reasons[item.id] || ''}
                    placeholder="Why? (required, recorded with your name)"
                    onChange={(e) => setReasons((r) => ({ ...r, [item.id]: e.target.value }))} />

          <div className="row wrap" style={{ gap: 10, marginTop: 8 }}>
            {DECISIONS.map((d) => (
              <button key={d.key} className={`big ${d.cls}`} disabled={busy === item.id}
                      title={d.hint} onClick={() => decide(item, d.key)}>
                {d.label}
              </button>
            ))}
            <Link className="ghost" to={`/triage?case=${item.case_id}`}>Open the case</Link>
          </div>
        </section>
      ))}
    </div>
  )
}
