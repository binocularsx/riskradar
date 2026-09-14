import { useState } from 'react'

import { api, clock, when } from '../lib/api'
import { Banner } from './ui'

/**
 * The regulator's clocks on a case (WP-05, D71).
 *
 * Nothing runs until the customer reports: a case the system detected is a
 * detection, not a complaint. Once a report is recorded, every CBN obligation
 * shows its deadline, and each one stops when the desk records the moment it
 * was met. Moments are recorded once and never edited.
 */

const CHANNELS = ['CONTACT_CENTRE', 'BRANCH', 'MOBILE_APP', 'WEB', 'EMAIL', 'USSD']

const MILESTONE_FOR = {
  ACKNOWLEDGE_REPORT: 'ACKNOWLEDGED',
  NOTIFY_COUNTERPARTY: 'COUNTERPARTY_NOTIFIED',
  CONCLUDE_INVESTIGATION: 'INVESTIGATION_CONCLUDED',
  REIMBURSE_AFTER_INVESTIGATION: 'REIMBURSED',
}

const STATE = {
  NOT_STARTED: { text: 'not started', cls: '' },
  RUNNING: { text: null, cls: 'sla-OK' },
  DUE: { text: null, cls: 'sla-DUE' },
  BREACHED: { text: null, cls: 'sla-BREACHED' },
  MET: { text: 'met', cls: 'sla-OK' },
  MET_LATE: { text: 'met late', cls: 'sla-DUE' },
  NOT_APPLICABLE: { text: 'not owed', cls: '' },
}

// <input type="datetime-local"> gives local wall time with no offset; the API
// refuses naive timestamps, so attach the browser's offset explicitly.
function withOffset(local) {
  const d = new Date(local)
  return Number.isNaN(d.getTime()) ? null : d.toISOString()
}

function nowLocal() {
  const d = new Date()
  d.setMinutes(d.getMinutes() - d.getTimezoneOffset())
  return d.toISOString().slice(0, 16)
}

export default function RegulatoryClocks({ caseRow, clocks, user, onUpdated }) {
  const [reportedAt, setReportedAt] = useState(nowLocal)
  const [channel, setChannel] = useState('CONTACT_CENTRE')
  const [institution, setInstitution] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  const can = (p) => user.permissions.includes(p)
  if (!caseRow) return null

  async function run(call) {
    setBusy(true); setError(null)
    try { onUpdated(await call()) } catch (e) { setError(e.message) } finally { setBusy(false) }
  }

  if (!caseRow.first_reported_at) {
    return (
      <div className="card" style={{ marginTop: 14 }}>
        <h3>Regulatory clocks</h3>
        <p className="dim" style={{ fontSize: 12.5, marginTop: -4 }}>
          No customer report on this case, so the CBN scam clocks have not started. When the
          customer reports being defrauded, record it here: the refund clock counts from that moment.
        </p>
        {error && <Banner kind="error">{error}</Banner>}
        {can('cases:review') && (
          <div className="row wrap" style={{ gap: 8 }}>
            <input type="datetime-local" value={reportedAt} max={nowLocal()} style={{ width: 200 }}
                   aria-label="Reported at" onChange={(e) => setReportedAt(e.target.value)} />
            <select value={channel} style={{ width: 160 }} aria-label="Reported through"
                    onChange={(e) => setChannel(e.target.value)}>
              {CHANNELS.map((c) => <option key={c} value={c}>{c.replace(/_/g, ' ').toLowerCase()}</option>)}
            </select>
            <input placeholder="Receiving institution (optional)" value={institution} style={{ width: 220 }}
                   onChange={(e) => setInstitution(e.target.value)} />
            <button className="primary" disabled={busy || !withOffset(reportedAt)}
                    onClick={() => run(() => api.recordReport(caseRow.id, {
                      reported_at: withOffset(reportedAt),
                      channel,
                      counterparty_institution: institution.trim() || null,
                    }))}>
              Record customer report
            </button>
          </div>
        )}
      </div>
    )
  }

  const needsInstitution = !caseRow.counterparty_institution

  return (
    <div className="card" style={{ marginTop: 14 }}>
      <div className="between">
        <h3 style={{ margin: 0 }}>Regulatory clocks</h3>
        <span className="dim" style={{ fontSize: 11.5 }}>
          reported {when(caseRow.first_reported_at)} via {String(caseRow.report_channel).replace(/_/g, ' ').toLowerCase()}
          {' '}· policy v{caseRow.clock_policy_version}
        </span>
      </div>
      {error && <Banner kind="error">{error}</Banner>}
      {needsInstitution && can('cases:review') && (
        <div className="row" style={{ marginTop: 10, gap: 8 }}>
          <input placeholder="Receiving institution, needed to record the notification" value={institution}
                 style={{ maxWidth: 360 }} onChange={(e) => setInstitution(e.target.value)} />
        </div>
      )}
      <div className="table-scroll" style={{ marginTop: 10 }}>
        <table>
          <thead>
            <tr><th>Obligation</th><th>Limit</th><th>Due</th><th>State</th><th /></tr>
          </thead>
          <tbody>
            {clocks.map((c) => {
              const s = STATE[c.state] || { text: c.state, cls: '' }
              const milestone = MILESTONE_FOR[c.code]
              const live = ['RUNNING', 'DUE', 'BREACHED'].includes(c.state)
              const refund = milestone === 'REIMBURSED'
              const allowed = refund ? can('cases:close') : can('cases:review')
              return (
                <tr key={c.code}>
                  <td>
                    {c.obligation}
                    {c.owner === 'CUSTOMER' && <span className="tag"> · the customer's</span>}
                    {c.estimated_holidays?.length > 0 && (
                      <div className="dim" style={{ fontSize: 11 }}>
                        counts {c.estimated_holidays.join(', ')} as a holiday; date not yet declared
                      </div>
                    )}
                    {!c.calendar_complete && (
                      <div style={{ fontSize: 11, color: 'var(--warn)' }}>
                        holiday calendar not loaded for part of this period; deadline may be early
                      </div>
                    )}
                  </td>
                  <td className="mono dim">{c.limit}</td>
                  <td className="mono dim">{c.due_at ? when(c.due_at) : '—'}</td>
                  <td>
                    <span className={`sla ${s.cls}`}>
                      {s.text ?? clock(c.remaining_minutes)}
                    </span>
                    {c.met_at && <div className="dim" style={{ fontSize: 11 }}>{when(c.met_at)}</div>}
                  </td>
                  <td>
                    {milestone && live && allowed && (
                      <button disabled={busy || (milestone === 'COUNTERPARTY_NOTIFIED' && needsInstitution && !institution.trim())}
                              onClick={() => run(() => api.recordMilestone(caseRow.id, {
                                milestone,
                                counterparty_institution: institution.trim() || null,
                              }))}>
                        Record {refund ? 'reimbursement' : 'done'}
                      </button>
                    )}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
      <p className="dim" style={{ fontSize: 11.5, marginBottom: 0 }}>
        Values from the CBN exposure draft (26 Nov 2025), to be re-checked against the final circular.
        Working days skip weekends and declared public holidays, counted in Lagos time. A bank clock that
        runs out is recorded and the case is escalated to Fraud Ops.
      </p>
    </div>
  )
}
