import { useState } from 'react'

import { api, clock, when } from '../lib/api'
import { Banner } from './ui'
import { sayLower } from '../lib/words'

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

const SYNC_LABEL = {
  PENDING: 'waiting for the industry connection',
  SENT: 'shared with the industry watch-list',
  FAILED: 'industry sync failed',
  NOT_SHAREABLE: 'not shared: no BVN known',
}

/** D75: where this flag stands with the industry watch-list, and what the BVN covers. */
function Scope({ flag }) {
  const latest = (flag.industry_sync || []).slice(-1)[0]
  return (
    <div className="dim" style={{ fontSize: 11.5, marginTop: 6 }}>
      {flag.scope === 'BVN'
        ? `BVN-level: covers ${flag.records_covered} customer record${flag.records_covered === 1 ? '' : 's'} with this BVN`
        : 'Customer-level: no BVN known for this customer'}
      {latest && (
        <>
          {' · '}
          <span title={latest.last_error || ''}>{latest.operation.toLowerCase()} {SYNC_LABEL[latest.status]}</span>
        </>
      )}
    </div>
  )
}

/** D75: flags other institutions placed on this customer's BVN. */
function Elsewhere({ identity, industryFlags }) {
  const active = industryFlags.filter((f) => f.active)
  return (
    <div style={{ marginTop: 12, paddingTop: 10, borderTop: '1px solid var(--bg-2)' }}>
      <div className="row wrap" style={{ gap: 8 }}>
        <span className="dim" style={{ fontSize: 11.5 }}>
          {identity?.bvn_known
            ? `BVN known (${String(identity.source).replace('_', ' ').toLowerCase()}), ${String(identity.verification_status).toLowerCase()}`
              + (identity.records_with_this_bvn > 1 ? ` · ${identity.records_with_this_bvn} customer records share it` : '')
            : 'No BVN known: the core has not been asked, or does not have it'}
        </span>
        {active.length > 0 && (
          <span className="pill override">flagged by {active.map((f) => f.institution_code).join(', ')}</span>
        )}
      </div>
      {industryFlags.map((f) => (
        <div key={f.external_ref} className="dim" style={{ fontSize: 11.5, padding: '2px 0' }}>
          {f.institution_code} · {sayLower({}, f.reason_code)} · {when(f.flagged_at)} to {when(f.expires_at)}
          {f.active ? '' : ' (ended)'}
        </div>
      ))}
    </div>
  )
}

export default function WatchlistFlag({ caseRow, flags, identity, industryFlags = [], user, onUpdated }) {
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
          <Scope flag={current} />
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

      <Elsewhere identity={identity} industryFlags={industryFlags} />
    </div>
  )
}
