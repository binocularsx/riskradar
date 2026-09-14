import { useState } from 'react'

import { api, clock, when } from '../lib/api'
import { Banner } from './ui'

/**
 * The twenty-four hour flag (WP-06, D73).
 *
 * CBN's BVN addendum (in force 1 May 2026) lets the bank place a customer on a
 * temporary watch-list for at most 24 hours while it contacts them. The panel
 * leads with the obligation — reach the customer — because a flag that expires
 * with nobody having called is the failure this control exists to prevent.
 * Risk Radar records the flag; the bank applies it to the customer's BVN.
 */

const CONTACT_LABEL = {
  PENDING: { text: 'contact the customer', cls: 'sla-OK' },
  DUE: { text: 'contact due', cls: 'sla-DUE' },
  CONTACTED: { text: 'customer contacted', cls: 'sla-OK' },
  MISSED: { text: 'contact missed', cls: 'sla-BREACHED' },
  NOT_NEEDED: { text: 'cleared before contact', cls: '' },
}

const OUTCOME_LABEL = {
  CUSTOMER_CONFIRMED_GENUINE: 'customer confirmed it was them',
  CUSTOMER_REPORTED_FRAUD: 'customer reported fraud',
}

export default function WatchlistFlag({ caseRow, flags, user, onUpdated }) {
  const [reason, setReason] = useState('')
  const [hours, setHours] = useState(24)
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  const can = (p) => user.permissions.includes(p)
  if (!caseRow) return null

  const current = flags.find((f) => f.state === 'ACTIVE')
  const past = flags.filter((f) => f !== current)

  async function run(call) {
    setBusy(true); setError(null)
    try { await call(); setNote(''); setReason(''); onUpdated() } catch (e) { setError(e.message) } finally { setBusy(false) }
  }

  return (
    <div className="card" style={{ marginTop: 14 }}>
      <div className="between">
        <h3 style={{ margin: 0 }}>24-hour watch-list flag</h3>
        {current && <span className="pill escalate">flagged · {clock(current.remaining_minutes)}</span>}
      </div>
      {error && <Banner kind="error">{error}</Banner>}

      {current ? (
        <>
          <p style={{ fontSize: 13, margin: '10px 0 6px' }}>
            Placed {when(current.placed_at)} by {current.placed_by_name}, ends {when(current.expires_at)}.{' '}
            <span className="muted">{current.reason}</span>
          </p>
          <div className="row wrap" style={{ gap: 8 }}>
            <span className={`sla ${CONTACT_LABEL[current.contact_state].cls}`}>
              {CONTACT_LABEL[current.contact_state].text}
            </span>
            {current.contact_outcome && (
              <span className="muted" style={{ fontSize: 12 }}>
                {OUTCOME_LABEL[current.contact_outcome]} · {when(current.customer_contacted_at)}
              </span>
            )}
          </div>
          <textarea style={{ marginTop: 10, minHeight: 44 }} placeholder="Note (optional, saved to the case)"
                    value={note} onChange={(e) => setNote(e.target.value)} />
          <div className="row wrap" style={{ gap: 8, marginTop: 8 }}>
            {!current.customer_contacted_at && can('cases:review') && Object.entries(OUTCOME_LABEL).map(([key, label]) => (
              <button key={key} disabled={busy}
                      onClick={() => run(() => api.flagContact(current.id, { outcome: key, note: note.trim() || null }))}>
                Reached: {label}
              </button>
            ))}
            {can('cases:escalate') && (
              <button className="ghost" disabled={busy}
                      onClick={() => run(() => api.liftFlag(current.id, { note: note.trim() || null }))}>
                Lift now: customer cleared
              </button>
            )}
          </div>
        </>
      ) : (
        <>
          <p className="dim" style={{ fontSize: 12.5, marginTop: 6 }}>
            No flag in force. A flag lasts at most 24 hours, ends on its own, and obliges the bank to contact
            the customer before it does. It does not change any score.
          </p>
          {caseRow.state !== 'CLOSED' && can('cases:escalate') && (
            <div className="row wrap" style={{ gap: 8 }}>
              <input placeholder="Why this customer is being flagged" value={reason} style={{ flex: 1, minWidth: 220 }}
                     onChange={(e) => setReason(e.target.value)} />
              <select value={hours} style={{ width: 110 }} aria-label="Hours"
                      onChange={(e) => setHours(Number(e.target.value))}>
                {[24, 12, 6, 2].map((h) => <option key={h} value={h}>{h} hours</option>)}
              </select>
              <button className="primary" disabled={busy || reason.trim().length < 5}
                      onClick={() => run(() => api.placeFlag(caseRow.id, { reason: reason.trim(), hours }))}>
                Place flag
              </button>
            </div>
          )}
        </>
      )}

      {past.length > 0 && (
        <div style={{ marginTop: 12 }}>
          {past.map((f) => (
            <div key={f.id} className="dim" style={{ fontSize: 11.5, padding: '3px 0' }}>
              {when(f.placed_at)} · {f.state === 'LIFTED' ? `lifted ${when(f.lifted_at)}` : `expired ${when(f.expires_at)}`}
              {' '}· <span className={`sla ${CONTACT_LABEL[f.contact_state].cls}`}>{CONTACT_LABEL[f.contact_state].text}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
